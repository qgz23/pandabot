# -*- coding: utf-8 -*-
"""
群角色池存储（今日老婆的素材来源）
================================================================
群友上传「角色名 + 图片」，存成本群自己的池子，之后 /今日老婆 从池里抽。

**图片存原图**：不裁剪、不缩放、不二次编码。抽到的图直接原样发出去，
所以用户传的是什么画质，群友看到的就是什么画质。
只有两种情况会动图：
    1. 格式 QQ 不认（webp/bmp/tiff）→ 转 PNG 无损兜底；
    2. 文件大过发送上限（默认 5MB）→ 等比缩小 + 降质量，**绝不裁剪**
       （压缩可以，改构图不行）。

为什么不用数据库：
    1. **图片必须落本地**。QQ 消息里的图片 url 有时效（几分钟到几小时），
       存 url 等于存了一个明天就会失效的链接，必须当场下载。
    2. 图片一落地，「元数据 + 文件」天然是一对，放同一个目录最不容易脱节
       （数据库里删了一条记录但图片还在，或反过来，都是很烦的脏数据）。
    3. 这是**群级内容**，不是用户数据。users 表是按 session_id 存个人字段的，
       塞一个列表进去会把搭档的表设计搞乱。

存储结构：
    data/waifu/{group_openid}.json           本群角色列表
    data/waifu/img/{group_openid}/{id}.jpg   角色图（扩展名按原格式，可能是 .jpg/.png/.gif）

**不依赖 nonebot**，可脱离 bot 直接测。
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx
from PIL import Image

MAX_IMAGE_BYTES = 8 * 1024 * 1024      # 单张上传上限 8MB
MAX_SEND_BYTES = 5 * 1024 * 1024       # 发送上限，超过才压缩
MAX_EDGE = 2560                        # 压缩时的长边上限（QQ 里用不到更多像素）
MAX_ROLES_PER_GROUP = 300              # 单群记录上限（同一角色的多张图各算一条）
MAX_NAME_LEN = 24
MAX_LINE_LEN = 80                      # 角色台词长度
MAX_VARIANTS_PER_UPLOADER = 5          # 同一个人对同一个角色最多传几张图

# QQ 能直接显示的格式：原样存、原样发，一个字节都不动
_RAW_OK = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif"}


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    image: str          # 文件名，例如 a1b2c3d4.jpg
    line: str = ""      # 角色出现时说的话（上传者填）
    by: str = ""        # 上传者 openid
    by_name: str = ""   # 上传者昵称（展示用）
    at: str = ""        # 上传时间 ISO


class RoleError(Exception):
    """业务异常。kind 用于上层决定提示文案。"""

    def __init__(self, message: str, kind: str = "unknown"):
        super().__init__(message)
        self.kind = kind


class RoleStore:
    def __init__(self, root: str | Path = "data/waifu") -> None:
        self.root = Path(root)
        self._lock = asyncio.Lock()          # 串行化 JSON 读写，避免并发上传互相覆盖
        self._client: httpx.AsyncClient | None = None

    # ---------------- 路径 ----------------
    def _json_path(self, group: str) -> Path:
        return self.root / f"{group}.json"

    def image_path(self, group: str, filename: str) -> Path:
        return self.root / "img" / group / filename

    # ---------------- 下载 ----------------
    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=8.0),
                follow_redirects=True,
                headers={"User-Agent": "pandabot/0.1 (+nonebot2)"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def download(self, url: str) -> bytes:
        """从 QQ 的临时图片地址把图拉下来。地址会过期，所以必须上传时立刻拉。"""
        if not url:
            raise RoleError("没有拿到图片地址", "no_url")
        try:
            client = await self._ensure_client()
            resp = await client.get(url)
        except Exception as e:
            raise RoleError(f"图片下载失败（{e.__class__.__name__}）", "download") from e

        if resp.status_code != 200:
            raise RoleError(f"图片下载失败（HTTP {resp.status_code}）", "download")

        data = resp.content
        if not data:
            raise RoleError("图片是空的", "download")
        if len(data) > MAX_IMAGE_BYTES:
            raise RoleError(f"图片太大了（{len(data) / 1024 / 1024:.1f}MB），换张小点的", "too_large")
        return data

    # ---------------- 读写 JSON ----------------
    def _read(self, group: str) -> list[dict]:
        path = self._json_path(group)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            return []       # 文件坏了当作空池，不阻断抽签
        items = raw.get("roles") if isinstance(raw, dict) else raw
        return [r for r in (items or []) if isinstance(r, dict) and r.get("id")]

    def _write(self, group: str, roles: list[dict]) -> None:
        path = self._json_path(group)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"group": group, "roles": roles}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        tmp.replace(path)       # 原子替换，避免写一半进程被杀留下坏 JSON

    # ---------------- 业务 ----------------
    async def list_roles(self, group: str) -> list[Role]:
        if not group:
            return []
        async with self._lock:
            items = await asyncio.to_thread(self._read, group)
        return [Role(**{k: str(v) for k, v in item.items() if k in Role.__annotations__})
                for item in items]

    async def count(self, group: str) -> int:
        return len(await self.list_roles(group))

    async def add(
        self,
        group: str,
        name: str,
        image_bytes: bytes,
        *,
        line: str = "",
        uploader: str = "",
        uploader_name: str = "",
    ) -> Role:
        """新增一条角色图。

        同一角色名**可以有多条记录**（不同群友传同一角色的不同图），
        抽签时会从这个角色的图池里随机抽一张。
        但同一个人对同一角色最多传 MAX_VARIANTS_PER_UPLOADER 张，防止有人刷屏。
        """
        if not group:
            raise RoleError("这个功能要在群里用", "no_group")

        name = (name or "").strip()
        if not name:
            raise RoleError("请给我一个角色名字，例如：/上传老婆 雪之下雪乃|我不需要朋友", "bad_name")
        if len(name) > MAX_NAME_LEN:
            raise RoleError(f"名字太长了，控制在 {MAX_NAME_LEN} 字以内", "bad_name")

        line = (line or "").strip()
        if len(line) > MAX_LINE_LEN:
            raise RoleError(f"台词太长了，控制在 {MAX_LINE_LEN} 字以内", "bad_line")

        if not image_bytes:
            raise RoleError("图片是空的", "bad_image")

        # 只做格式识别（顺带验证这确实是一张能解码的图），不裁不压
        try:
            ext, payload = await asyncio.to_thread(sniff_image, image_bytes)
        except Exception as e:
            raise RoleError("这张图我读不出来，换一张试试", "bad_image") from e

        async with self._lock:
            items = await asyncio.to_thread(self._read, group)

            mine = [
                i for i in items
                if str(i.get("name", "")).strip() == name and str(i.get("by") or "") == str(uploader or "")
            ]
            if len(mine) >= MAX_VARIANTS_PER_UPLOADER:
                raise RoleError(
                    f"你对「{name}」已经传了 {MAX_VARIANTS_PER_UPLOADER} 张了，"
                    "换别人来传吧，或者换个角色",
                    "too_many",
                )

            if len(items) >= MAX_ROLES_PER_GROUP:
                raise RoleError(f"本群角色池满了（{MAX_ROLES_PER_GROUP} 条），先删几条再加", "full")

            role = Role(
                id=uuid.uuid4().hex[:10],
                name=name,
                image=f"{uuid.uuid4().hex[:10]}{ext}",
                line=line,
                by=str(uploader or ""),
                by_name=str(uploader_name or ""),
                at=datetime.now().isoformat(timespec="seconds"),
            )

            folder = self.root / "img" / group
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / role.image
            tmp = target.with_suffix(target.suffix + ".tmp")
            await asyncio.to_thread(tmp.write_bytes, payload)
            await asyncio.to_thread(tmp.replace, target)   # 原子落盘

            items.append(asdict(role))
            await asyncio.to_thread(self._write, group, items)

        return role

    async def remove(self, group: str, keyword: str, *, requester: str, admin: bool = False) -> tuple[int, str]:
        """按名字删掉**自己上传的**所有图（同名可能是多人传的）。

        返回 (删除条数, 角色名)。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            raise RoleError("要删哪个？例如：/删除老婆 雪之下雪乃", "bad_name")

        async with self._lock:
            items = await asyncio.to_thread(self._read, group)

            same_name = [i for i in items if str(i.get("name", "")).strip() == keyword]
            if not same_name:
                raise RoleError(f"本群没有叫「{keyword}」的角色", "not_found")

            mine = [
                i for i in same_name
                if admin or str(i.get("by") or "") == str(requester or "")
            ]
            if not mine:
                who = same_name[0].get("by_name") or "别人"
                raise RoleError(f"「{keyword}」是{who}传的，只有本人能删", "forbidden")

            doomed = {str(i.get("id")) for i in mine}
            items = [i for i in items if str(i.get("id")) not in doomed]
            await asyncio.to_thread(self._write, group, items)

        # 元数据删成功后再删图；图删失败最多留个孤儿文件，不影响使用
        for item in mine:
            try:
                path = self.image_path(group, str(item.get("image") or ""))
                if path.exists():
                    path.unlink()
            except OSError:
                pass

        return len(mine), keyword

    async def read_bytes(self, group: str, filename: str) -> bytes | None:
        """原始文件字节。找不到返回 None。"""
        path = self.image_path(group, filename or "")
        if not filename or not path.exists():
            return None
        try:
            return await asyncio.to_thread(path.read_bytes)
        except OSError:
            return None

    async def send_ready(
        self, group: str, filename: str, *, limit: int = MAX_SEND_BYTES
    ) -> bytes | None:
        """取出可以直接发出去的字节。

        没超限就是原图原样返回（零处理）；超了才压一次，
        且只等比缩小 + 降质量，绝不裁剪。
        """
        data = await self.read_bytes(group, filename)
        if data is None or len(data) <= limit:
            return data
        try:
            return await asyncio.to_thread(shrink, data, limit)
        except Exception:
            return data     # 压不动就硬发，让 QQ 那边报错，总比静默失败好

    def stats(self) -> str:
        if not self.root.exists():
            return "角色池：空"
        groups = len(list(self.root.glob("*.json")))
        img_dir = self.root / "img"
        imgs = len([p for p in img_dir.rglob("*") if p.is_file()]) if img_dir.exists() else 0
        return f"角色池：{groups} 个群，{imgs} 张图"


def sniff_image(data: bytes) -> tuple[str, bytes]:
    """识别图片格式，返回 (扩展名, 要落盘的字节)。

    QQ 认的格式原样返回 —— 用户要的是原图，多一次解码编码就是多一次画质损失。
    webp/bmp/tiff 这类 QQ 不认的转 PNG（无损），保证能发出去。
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()          # 真正解码一次，坏图/截断图在这里暴露
        fmt = (img.format or "").upper()
    except Exception as e:
        raise ValueError("无法解码的图片") from e

    if fmt in _RAW_OK:
        return _RAW_OK[fmt], data

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return ".png", buf.getvalue()


def shrink(data: bytes, limit: int) -> bytes:
    """把图压到 limit 以内：等比缩尺寸 + 降 JPEG 质量，绝不裁剪。

    只在超过发送上限时用。顺序是**先限长边、再降质量、最后才继续缩**——
    反过来的话，一张 4000x3000 的图会被压成 q30 的原尺寸图，
    体积是达标了，但满屏马赛克。先缩到 2560 再 q90 观感好得多，
    而 QQ 聊天窗口根本用不到 4000 像素。
    """
    img = Image.open(io.BytesIO(data))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        base = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        base.paste(rgba, mask=rgba.split()[-1])
        img = base
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if max(img.size) > MAX_EDGE:
        k = MAX_EDGE / max(img.size)
        size = (max(1, round(img.width * k)), max(1, round(img.height * k)))
        img = img.resize(size, Image.Resampling.LANCZOS)

    best = b""
    for scale in (1.0, 0.85, 0.7, 0.55, 0.4, 0.3, 0.2):
        if scale == 1.0:
            frame = img
        else:
            size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            frame = img.resize(size, Image.Resampling.LANCZOS)
        for quality in (90, 82, 74, 65, 55, 42):
            buf = io.BytesIO()
            frame.save(buf, "JPEG", quality=quality, optimize=True)
            best = buf.getvalue()
            if len(best) <= limit:
                return best
    return best


# 全局单例
roles = RoleStore()
