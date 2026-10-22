# -*- coding: utf-8 -*-
"""
曲绘封面缓存
================================================================
「成分图」和普通文字卡唯一的本质差别就是：**每格要一张曲绘**。
所以整个功能的性能瓶颈全在这一层。

实测事实（2026-10-22）：
    封面地址  https://www.diving-fish.com/covers/{song_id:05d}.png
    单张大小  280~420 KB，尺寸 400x400 RGBA
    单张耗时  0.4~3.4 秒（波动很大）
    → 50 张串行要 30~60 秒，直接卡死命令
    → 部分曲目 404（新曲还没上传曲绘），必须兜底

所以做了三件事：
    1. **并发下载**：asyncio.gather + 信号量限流（默认 12 路）
    2. **两级缓存**：内存 LRU → 磁盘小图（缩到 168px 后约 20KB）→ 才走网络
    3. **404 负缓存**：确认没封面的 id 写个 .404 标记，以后不再白跑一次网络

**不依赖 nonebot**，可脱离 bot 单独测。
"""

from __future__ import annotations

import asyncio
import io
from collections import OrderedDict
from pathlib import Path

import httpx
from PIL import Image

COVER_URL = "https://www.diving-fish.com/covers/{sid:05d}.png"

# 卡片里一格其实只有 200x104，但存 168 是为了以后放大图 / 换更大版式时不用重下
CACHE_SIZE = 168


class CoverCache:
    """曲绘封面的两级缓存。

    用法：
        covers = await pool.load_many([11823, 11861, ...])
        # -> {11823: <PIL.Image 168x168>, ...}，拿不到的 id 不会出现在结果里
    """

    def __init__(
        self,
        cache_dir: str | Path = "data/covers",
        *,
        size: int = CACHE_SIZE,
        concurrency: int = 16,
        timeout: float = 6.0,
        mem_limit: int = 300,
    ) -> None:
        self.dir = Path(cache_dir)
        self.size = size
        self.concurrency = concurrency
        self.timeout = timeout
        self.mem_limit = mem_limit

        self._mem: OrderedDict[int, Image.Image] = OrderedDict()
        self._sem = asyncio.Semaphore(concurrency)
        # singleflight：同一个 id 正在下载时，后来的调用等同一个任务，
        # 避免「两个群同时查 b50」把同一批图下载两遍
        self._inflight: dict[int, asyncio.Task] = {}
        # 超时后被"放在后台继续跑"的任务，必须留引用，否则可能被 GC 掉
        self._background: set[asyncio.Task] = set()
        self._client: httpx.AsyncClient | None = None

    # ---------------- 基础设施 ----------------
    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=6.0),
                headers={"User-Agent": "pandabot/0.1 (+nonebot2)"},
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        # 后台还在下的一律取消，避免退出时刷一堆
        # "Task was destroyed but it is pending" 的警告
        for task in list(self._background):
            task.cancel()
        self._background.clear()
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _path(self, sid: int) -> Path:
        return self.dir / f"{sid:05d}.png"

    def _missing_path(self, sid: int) -> Path:
        return self.dir / f"{sid:05d}.404"

    def _remember(self, sid: int, img: Image.Image) -> None:
        self._mem[sid] = img
        self._mem.move_to_end(sid)
        while len(self._mem) > self.mem_limit:
            self._mem.popitem(last=False)  # 淘汰最久没用的，防止内存无限涨

    # ---------------- 单张加载 ----------------
    async def load(self, sid: int) -> Image.Image | None:
        """取一张封面。拿不到返回 None（调用方画占位块）。"""
        if sid <= 0:
            return None

        cached = self._mem.get(sid)
        if cached is not None:
            self._mem.move_to_end(sid)
            return cached

        task = self._inflight.get(sid)
        if task is None:
            task = asyncio.create_task(self._load_uncached(sid))
            self._inflight[sid] = task
            task.add_done_callback(lambda _t, s=sid: self._inflight.pop(s, None))

        try:
            return await task
        except Exception:
            # 单张失败不能拖垮整张图，交给调用方画占位
            return None

    async def _load_uncached(self, sid: int) -> Image.Image | None:
        # 1) 磁盘命中：本地小图，几毫秒
        path = self._path(sid)
        if path.exists():
            img = await asyncio.to_thread(_open_resized, path, self.size)
            if img is not None:
                self._remember(sid, img)
                return img

        # 2) 已知无封面，不再浪费一次网络请求
        if self._missing_path(sid).exists():
            return None

        # 3) 走网络
        async with self._sem:
            try:
                client = await self._ensure_client()
                resp = await client.get(COVER_URL.format(sid=sid))
            except Exception:
                return None

        if resp.status_code == 404:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._missing_path(sid).write_bytes(b"")
            return None
        if resp.status_code != 200 or not resp.content:
            return None

        try:
            img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        except Exception:
            return None

        img = _resize(img, self.size)
        self._remember(sid, img)

        # 4) 落盘。先写临时文件再改名，避免进程被杀时留下半张图的坏缓存
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            await asyncio.to_thread(_save_png, img, tmp)
            await asyncio.to_thread(tmp.replace, path)
        except Exception:
            pass  # 缓存写失败不影响本次出图

        return img

    # ---------------- 批量加载 ----------------
    async def load_many(
        self, sids: list[int], *, budget: float | None = None
    ) -> dict[int, Image.Image]:
        """并发取一批封面，返回 {sid: Image}。拿不到的直接不出现在结果里。

        budget：时间预算（秒）。有预算时，到点就把**已经下好的**交出去，
        剩下的不取消，继续在后台跑完并落盘。

        为什么这么做：实测 50 张封面首次下载要 14.6 秒，让用户干等十几秒
        是不可接受的。加了预算之后：
            第一次查询  ~5 秒出图（部分格子是占位色块）
            后台继续    ~10 秒内补齐并写盘
            第二次查询  0.3 秒，完整图
        这是「渐进式可用」，比单纯加并发或者干等着都聪明。
        """
        unique = list(dict.fromkeys(int(s) for s in sids if s and int(s) > 0))
        if not unique:
            return {}

        tasks = {asyncio.create_task(self.load(s)): s for s in unique}

        if budget is None:
            await asyncio.wait(tasks)
            done = set(tasks)
        else:
            done, pending = await asyncio.wait(tasks, timeout=budget)
            for t in pending:
                self._background.add(t)
                t.add_done_callback(self._background.discard)

        result: dict[int, Image.Image] = {}
        for task in done:
            if task.cancelled():
                continue
            img = task.result() if not task.exception() else None
            if img is not None:
                result[tasks[task]] = img
        return result

    def stats(self) -> str:
        """给日志用：有多少已缓存。"""
        if not self.dir.exists():
            return "封面缓存：空"
        n = sum(1 for _ in self.dir.glob("*.png"))
        miss = sum(1 for _ in self.dir.glob("*.404"))
        return f"封面缓存：{n} 张，无封面记录 {miss} 个"


def _resize(img: Image.Image, size: int) -> Image.Image:
    if img.size == (size, size):
        return img
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _open_resized(path: Path, size: int) -> Image.Image | None:
    try:
        with Image.open(path) as im:
            return _resize(im.convert("RGBA"), size)
    except Exception:
        # 缓存文件坏了就当没有，下次会重新下载
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def _save_png(img: Image.Image, path: Path) -> None:
    img.save(path, format="PNG", optimize=True)


# 全局单例：跨命令、跨群共用同一份内存与磁盘缓存
covers = CoverCache()
