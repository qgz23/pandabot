# -*- coding: utf-8 -*-
"""
舞萌查分服务层（水鱼查分器 diving-fish）
================================================================
本模块 **不 import 任何 nonebot 相关代码**，可以脱离 bot 单独运行测试：
    python -c "import asyncio; from src.plugins.maimai.service import maimai; \
               print(asyncio.run(maimai.fetch_b50('turou')).rating)"

为什么不用 maimai-py 封装库：
    它的新版依赖 maimai-ffi（原生编译扩展），没有 Python 3.15 的 Windows 轮子，
    pip 会一路回溯装成几年前的旧版本，API 完全对不上。这里直接打查分器的 HTTP 接口。

接口（已实测）：
    GET  /api/maimaidxprober/music_data    全曲库 ~1400 首，约 900KB
    POST /api/maimaidxprober/query/player  body {"username": "...", "b50": true}
公开 B50 查询**不需要 token**；查 QQ 号绑定或他人完整成绩需要 Developer-Token，
放在 .env.prod 里（不进 Git），由插件层读出来传进 configure()。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx

DIVINGFISH_BASE = "https://www.diving-fish.com/api/maimaidxprober"

# 难度下标 0~4 对应的名字（水鱼用 level_index 表示）
DIFF_NAMES = ("BASIC", "ADVANCED", "EXPERT", "MASTER", "Re:MASTER")

# 达成率评级 → 展示文本
_RATE_LABELS = {
    "sssp": "SSS+", "sss": "SSS", "ssp": "SS+", "ss": "SS",
    "sp": "S+", "s": "S", "aaa": "AAA", "aa": "AA", "a": "A",
    "bbb": "BBB", "bb": "BB", "b": "B", "c": "C", "d": "D",
}


class MaimaiError(Exception):
    """业务异常。kind 用来让上层决定提示文案，不用去解析字符串。"""

    def __init__(self, message: str, kind: str = "unknown"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ChartScore:
    """单条成绩。"""

    title: str
    song_id: int
    chart_type: str     # SD / DX（谱面类型，不是新旧曲）
    level: str          # "14+"
    level_index: int    # 0~4
    ds: float           # 定数
    achievements: float # 达成率
    ra: int             # 单谱 Rating
    rate: str           # 评级原始值 sssp/sss/...
    fc: str             # full combo 标记
    fs: str             # full sync 标记
    dx_score: int

    @property
    def diff_name(self) -> str:
        if 0 <= self.level_index < len(DIFF_NAMES):
            return DIFF_NAMES[self.level_index]
        return "?"

    @property
    def rate_label(self) -> str:
        return _RATE_LABELS.get(self.rate, self.rate.upper() or "-")


@dataclass(frozen=True)
class B50Result:
    """一次 B50 查询的完整结果。b15 = 新曲最佳 15，b35 = 旧曲最佳 35。"""

    username: str
    nickname: str
    rating: int
    additional_rating: int
    plate: str
    b15: list[ChartScore] = field(default_factory=list)
    b35: list[ChartScore] = field(default_factory=list)

    @property
    def total_ra(self) -> int:
        return sum(c.ra for c in self.b15) + sum(c.ra for c in self.b35)


@dataclass(frozen=True)
class SongHit:
    """曲库里的一个条目。同一首歌 SD / DX 各算一条。"""

    id: int
    title: str
    chart_type: str
    ds: list[float]
    levels: list[str]
    artist: str = ""


class MaimaiService:
    """查分服务。全局只建一个实例（复用连接池 + 曲库缓存）。"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._timeout: float = 20.0
        self._token: str = ""
        self._songs: list[SongHit] = []
        self._songs_at: float = 0.0
        self._songs_ttl: float = 6 * 3600
        self._dirty: bool = False
        self._lock = asyncio.Lock()

    # ---------------- 配置 ----------------
    def configure(self, *, timeout: float = 20.0, token: str = "", song_ttl: float = 21600) -> None:
        """由插件层在启动 / 配置热更新时调用。改了超时就标记需要重建连接池。"""
        timeout = max(3.0, float(timeout))
        song_ttl = max(60.0, float(song_ttl))
        if abs(timeout - self._timeout) > 1e-6:
            self._dirty = True
        if abs(song_ttl - self._songs_ttl) > 1e-6:
            self._songs_ttl = song_ttl
        self._timeout = timeout
        self._token = (token or "").strip()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None and self._dirty:
            await self._client.aclose()
            self._client = None
            self._dirty = False
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=8.0),
                headers={"User-Agent": "pandabot/0.1 (+nonebot2)"},
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ---------------- 底层请求 ----------------
    async def _request(self, method: str, url: str, **kwargs):
        client = await self._ensure_client()
        if self._token:
            kwargs.setdefault("headers", {})["Developer-Token"] = self._token

        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as e:
            raise MaimaiError("查分器响应超时了，稍后再试一次", "timeout") from e
        except httpx.HTTPError as e:
            raise MaimaiError(f"连不上查分器（{e.__class__.__name__}），请检查网络", "network") from e

        if resp.status_code == 429:
            raise MaimaiError("查分器限流了，等一会儿再查吧", "rate_limit")

        if resp.status_code >= 400:
            msg = ""
            try:
                msg = str(resp.json().get("message") or "")
            except Exception:
                msg = resp.text[:120]
            raise MaimaiError(*_translate_error(resp.status_code, msg))

        try:
            return resp.json()
        except ValueError as e:
            raise MaimaiError("查分器返回了无法解析的数据", "parse") from e

    # ---------------- 业务：B50 ----------------
    async def fetch_b50(self, username: str) -> B50Result:
        username = (username or "").strip()
        if not username:
            raise MaimaiError("请提供水鱼查分器的用户名", "bad_request")

        data = await self._request(
            "POST", f"{DIVINGFISH_BASE}/query/player",
            json={"username": username, "b50": True},
        )
        if not isinstance(data, dict):
            raise MaimaiError("查分器返回格式异常", "parse")

        charts = data.get("charts") or {}
        b15 = [self._to_chart(c) for c in (charts.get("dx") or [])]
        b35 = [self._to_chart(c) for c in (charts.get("sd") or [])]

        return B50Result(
            # 用接口回传的规范用户名，避免用户输入的大小写不一致后面查不到
            username=str(data.get("username") or username),
            nickname=str(data.get("nickname") or data.get("username") or username),
            rating=int(data.get("rating") or 0),
            additional_rating=int(data.get("additional_rating") or 0),
            plate=str(data.get("plate") or ""),
            b15=b15,
            b35=b35,
        )

    @staticmethod
    def _to_chart(d: dict) -> ChartScore:
        def _num(v, cast=float, default=0):
            try:
                return cast(v)
            except (TypeError, ValueError):
                return cast(default)

        return ChartScore(
            title=str(d.get("title") or "?"),
            song_id=_num(d.get("song_id"), int),
            chart_type=str(d.get("type") or ""),
            level=str(d.get("level") or "-"),
            level_index=_num(d.get("level_index"), int),
            ds=_num(d.get("ds")),
            achievements=_num(d.get("achievements")),
            ra=_num(d.get("ra"), int),
            rate=str(d.get("rate") or ""),
            fc=str(d.get("fc") or ""),
            fs=str(d.get("fs") or ""),
            dx_score=_num(d.get("dxScore"), int),
        )

    # ---------------- 业务：曲库 / 查歌 ----------------
    async def songs(self, *, force: bool = False) -> list[SongHit]:
        """取全曲库。带内存缓存 + 双检锁，避免并发请求重复拉 900KB。"""
        if not force and self._is_fresh():
            return self._songs
        async with self._lock:
            if not force and self._is_fresh():
                return self._songs
            raw = await self._request("GET", f"{DIVINGFISH_BASE}/music_data")
            parsed: list[SongHit] = []
            for item in raw or []:
                if not isinstance(item, dict):
                    continue
                basic = item.get("basic_info") or {}
                parsed.append(
                    SongHit(
                        id=_safe_int(item.get("id")),
                        title=str(item.get("title") or basic.get("title") or "?"),
                        chart_type=str(item.get("type") or ""),
                        ds=[_safe_float(x) for x in (item.get("ds") or [])],
                        levels=[str(x) for x in (item.get("level") or [])],
                        artist=str(basic.get("artist") or ""),
                    )
                )
            self._songs = parsed
            self._songs_at = time.monotonic()
            return self._songs

    def _is_fresh(self) -> bool:
        return bool(self._songs) and (time.monotonic() - self._songs_at) < self._songs_ttl

    async def search(self, keyword: str, limit: int = 10) -> list[SongHit]:
        kw = (keyword or "").strip().lower()
        if not kw:
            raise MaimaiError("请输入要搜索的曲名关键词", "bad_request")
        songs = await self.songs()
        hits = [s for s in songs if kw in s.title.lower()]
        # 完全同名 > 前缀匹配 > 名字短的（更可能是用户想找的那首）
        hits.sort(key=lambda s: (s.title.lower() != kw, not s.title.lower().startswith(kw), len(s.title)))
        return hits[: max(1, limit)]


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _translate_error(status: int, raw: str) -> tuple[str, str]:
    """把查分器的英文错误翻译成人话，顺便给出 kind。"""
    low = (raw or "").lower()
    if "user not exists" in low or status == 404:
        return ("查分器里没有这个用户。请确认用户名是否正确，"
                "并且对方已经在水鱼查分器上传过成绩。"), "not_found"
    if "already" in low and "agree" in low:
        return "该用户还没在水鱼查分器同意用户协议，暂时查不了", "privacy"
    if "developer" in low or "token" in low or status == 403:
        return ("这个查询需要 Developer-Token。"
                "请管理员在 .env.prod 里配置 DF_DEV_TOKEN。"), "need_token"
    if "privacy" in low or "not allowed" in low:
        return "对方设置了隐私保护，不允许查询", "privacy"
    if not raw:
        return f"查分器返回了错误（HTTP {status}）", "unknown"
    return f"查分器返回：{raw}", "unknown"


# 全局单例：整条消息处理链共用同一个连接池和曲库缓存
maimai = MaimaiService()
