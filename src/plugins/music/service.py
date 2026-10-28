# -*- coding: utf-8 -*-
"""
点歌 —— 音源搜索与直链解析
================================================================
**搜索和取直链用的是完全不同的接口**，因为没有一个源能同时把两件事做好。

搜索走网易云官方老接口 ``music.163.com/api/search/get/web``：
    快（0.3~1.5 秒）、稳，而且返回真实时长（毫秒）和收费标记，
    比按文件大小反推准得多。这个接口不要签名、不要 cookie。

取直链是**两路并发**（``resolve()``），谁先给出地址用谁：

    1. ``api.qijieya.cn/meting/``                     0.3 秒、128k 完整版、
       会员歌也给 —— 主力
    2. ``music.163.com/api/song/enhance/player/url``  0.3 秒、带准确文件大小，
       但**只给免费歌**
    3. ``music-api.gdstudio.xyz``                     会员歌也能拿，但 1~13 秒，
       偶尔 503 / 返回截断 JSON / 干脆不响应

为什么不全用 gdstudio：它就是③，实测同一个地址有时 1.1 秒、有时 13.6 秒、
有时 30 秒不返回。而①给的完整版和它**一字节不差**，还快 10 倍。
③留着只是因为公共实例随时可能挂，多一路兜底。

**弃用过的源，别再搬回来**：``api.injahow.cn/meting/`` 快得诱人（0.2 秒），
但它对会员歌只返回 ~480KB 的**试听片段**（原曲 4MB），对免费歌又给 320k
完整版（13MB，超过直传上限）。行为不一致且无法从 302 看出是哪种，
拿它当主路径会出现「点一首歌只响 30 秒」这种最坏情况。

为什么官方 enhance 不能单用：它返回的 ``code`` 很直白 ——
200 = 有直链、**-110 = 要会员**、404 = 没版权。判断准，但会员歌一律拿不到。

为什么取直链要单独发请求：
    搜索结果里只有歌曲 ID，没有播放地址。而且直链是**带时间戳的临时地址**
    （网易云 CDN 的路径里嵌了 ``20261028171839`` 这样的时刻），过一阵就失效。
    所以只能短缓存，不能落盘存死。

为什么不落盘缓存音频文件：
    一首 128k 的歌 3~5MB，几个群一天点几十首就是几百 MB，
    而 QQ 的 file_info 本身有 ttl，存了也用不了多久。每次现取最省事。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

import httpx
from nonebot.log import logger

API = "https://music-api.gdstudio.xyz/api.php"
OFFICIAL_SEARCH = "https://music.163.com/api/search/get/web"
ENHANCE_URL = "https://music.163.com/api/song/enhance/player/url"
QIJIEYA = "https://api.qijieya.cn/meting/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
REFERER = "https://music.163.com/"

# 直链缓存：{(歌曲ID, 码率): (取到的时刻, Song)}
# 网易云直链一般能撑几小时，但保守一点，10 分钟就重取。
_URL_TTL = 600.0
_url_cache: dict[tuple[str, int], tuple[float, "Song"]] = {}


@dataclass
class Song:
    """一首歌的基本信息。``url`` / ``size`` / ``br`` 要 resolve 之后才有。"""

    id: str
    name: str
    artists: tuple[str, ...] = ()
    album: str = ""
    source: str = "netease"
    url: str = ""
    size: int = 0
    br: int = 0
    duration: float = 0.0

    @property
    def artist_text(self) -> str:
        if not self.artists:
            return "未知歌手"
        return " / ".join(a for a in self.artists if a) or "未知歌手"

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.id)

    def title(self) -> str:
        return f"{self.name} - {self.artist_text}"

    def size_text(self) -> str:
        if not self.size:
            return ""
        mb = self.size / 1048576
        return f"{mb:.1f}MB" if mb >= 1 else f"{self.size / 1024:.0f}KB"

    def duration_text(self) -> str:
        """按码率反推时长。没有 ffmpeg，读不了音频头，只能这么估。

        128kbps = 16KB/s，误差在几秒内，用来提示「这歌有点长」足够了。
        """
        if self.duration:
            return _mmss(self.duration)
        if not (self.size and self.br):
            return ""
        return _mmss(self.size / (self.br * 1000 / 8))


def _mmss(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _client(timeout: float = 25) -> httpx.AsyncClient:
    """统一的 httpx 客户端。

    ``trust_env=False`` 是必须的：httpx 默认会读 ``HTTP_PROXY`` 环境变量，
    本机挂着一个经常 502 的局域网代理，请求会被它拖到 ConnectTimeout。
    实测同一个地址 curl --noproxy 是通的、走代理就超时，说明是代理的问题。
    服务器上没有代理，这个设置不影响。
    """
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        trust_env=False,
        headers={"User-Agent": UA},
    )


async def _get(params: dict, retries: int = 0) -> object:
    """调接口。非 200、返回里带 detail、网络超时都当失败。

    超时 12 秒、**不重试**：实测这个接口 5 次里有 1 次 13 秒、1 次直接
    不响应。重试只会把等待翻倍，不如早点失败让用户再点一次
    —— 第二次多半 1 秒就回来了。
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with _client() as client:
                resp = await client.get(API, params=params)
                if resp.status_code != 200:
                    raise RuntimeError(f"接口返回 {resp.status_code}")
                data = resp.json()

            # 出错时是 {"detail": "..."}，不是列表，光看状态码发现不了
            if isinstance(data, dict) and data.get("detail"):
                raise RuntimeError(str(data["detail"]))
            return data
        except Exception as e:
            last = e
            if attempt < retries:
                logger.debug(f"点歌接口第 {attempt + 1} 次失败（{e!r}），重试")
                await asyncio.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"点歌接口失败：{last!r}")


def _as_artists(raw: object) -> tuple[str, ...]:
    """artist 字段有时是数组，有时是字符串，统一成元组。"""
    if isinstance(raw, str):
        return tuple(x.strip() for x in raw.split("/") if x.strip())
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return ()


async def _search_official(keyword: str) -> list[Song]:
    """网易云官方搜索。返回 Song 列表，带真实时长（秒）。

    这个老接口不需要签名、不需要 cookie，速度也快，是搜索的主路径。
    """
    params = {"s": keyword, "type": 1, "limit": 20, "offset": 0}
    async with _client(12) as client:
        resp = await client.get(OFFICIAL_SEARCH, params=params, headers={"Referer": REFERER})
        if resp.status_code != 200:
            raise RuntimeError(f"官方搜索返回 {resp.status_code}")
        data = resp.json()

    rows = ((data.get("result") or {}).get("songs")) or []
    songs: list[Song] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not sid or not name:
            continue
        songs.append(
            Song(
                id=sid,
                name=name,
                artists=tuple(
                    str(a.get("name") or "").strip()
                    for a in (row.get("artists") or [])
                    if isinstance(a, dict) and a.get("name")
                ),
                album=str((row.get("album") or {}).get("name") or "").strip(),
                source="netease",
                duration=(row.get("duration") or 0) / 1000,
            )
        )
    return songs


async def _search_aggregate(keyword: str, source: str) -> list[Song]:
    """聚合接口搜索。官方接口挂掉、或者要搜网易云以外的源时用它。"""
    data = await _get(
        {
            "types": "search",
            "source": source,
            "name": keyword,
            "count": 30,
            "pages": 1,
        }
    )
    if not isinstance(data, list):
        return []

    songs: list[Song] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not sid or not name:
            continue
        songs.append(
            Song(
                id=sid,
                name=name,
                artists=_as_artists(row.get("artist")),
                album=str(row.get("album") or "").strip(),
                source=str(row.get("source") or source),
            )
        )
    return songs


async def search(keyword: str, limit: int = 8, source: str = "netease") -> list[Song]:
    """按关键词搜歌。返回本地重排后的结果。

    为什么要自己重排：接口的排序是按「热度 + 相关度」给的，搜「起风了」还算准，
    但搜带变体的词（很多翻唱、钢琴版、DJ 版）就会被变体占满前排。
    这里按「歌名/歌手是否真的对上」重新打分，把变体压下去。

    为什么搜索优先走官方而不是聚合接口：
        聚合接口会 503、会返回截断的 JSON、会 40 秒不响应（都实测到了），
        重试一次就要几十秒，群里点个歌等半分钟没人受得了。
    """
    keyword = keyword.strip()
    if not keyword:
        return []

    songs: list[Song] = []
    if source == "netease":
        try:
            songs = await _search_official(keyword)
        except Exception as e:
            logger.debug(f"官方搜索不可用（{e!r}），转聚合接口")
    if not songs:
        songs = await _search_aggregate(keyword, source)

    # 同一首歌在结果里可能重复出现（不同专辑/合辑），按「歌名+歌手」去重
    unique: list[Song] = []
    seen: set[tuple[str, str]] = set()
    for s in songs:
        sig = (s.name, s.artist_text)
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(s)

    parts = [p for p in _SPLIT_RE.split(keyword.lower()) if p]
    scored = [(_relevance(s, keyword, parts), i, s) for i, s in enumerate(unique)]
    scored.sort(key=lambda t: (-t[0], t[1]))   # 同分保持接口原顺序
    return [s for _, _, s in scored[:limit]]


# 关键词里的分隔符：空格、逗号、斜杠都算
_SPLIT_RE = re.compile(r"[\s,，/、+]+")

# 变体版本降权。命中这些词的基本都是翻唱、伴奏或改编，
# 用户点歌十有八九要原版，把这些压下去比提升原版更有效。
_VARIANT_PENALTY = (
    ("cover", 55), ("翻唱", 55), ("翻自", 55), ("原唱", 50),
    ("伴奏", 65), ("remix", 40), ("钢琴", 40), ("吉他", 40),
    ("抖音", 35), ("纯音乐", 35), ("片段", 55), ("铃声", 55),
    ("现场", 25), ("live", 20), ("dj", 40), ("女声", 20), ("男声", 20),
)


def _relevance(song: Song, keyword: str, parts: list[str]) -> int:
    """给一首歌打分。分数越高越像「用户真正想点的那首」。"""
    kw = keyword.lower().strip()
    name = song.name.lower()
    artists = song.artist_text.lower()

    score = 0
    if name == kw:
        score += 100                     # 歌名一字不差
    elif name.startswith(kw):
        score += 40
    elif kw and kw in name:
        score += 25

    if kw and kw in artists:
        score += 60                      # 关键词本身是歌手名
    for p in parts:
        if p in artists:
            score += 45                  # 「周杰伦」对上歌手，权重比对上歌名高
        elif p in name:
            score += 12

    for word, penalty in _VARIANT_PENALTY:
        if word in name:
            score -= penalty

    # 歌名比关键词长出一大截，多半是「某某版」「某某 remix」这类变体
    if len(name) > len(kw) + 12:
        score -= 8
    return score


async def _try_qijieya(song: Song, bitrate: int = 128) -> tuple[str, int, int]:
    """Meting 公共实例 api.qijieya.cn，**取直链的主力**。

    实测 0.30~0.36 秒、连点 5 次全中，``br=128`` 给的完整版是 4096879 字节，
    和 gdstudio 给的**一字节不差**，但快 10 倍以上。会员歌也照给。

    缺点是 302 不携带文件大小，``size`` 只能留空，等下载时才知道有多大。
    这没关系 —— 上层本来就按「下下来量一下」来判断走直传还是 URL 上传。
    """
    if (song.source or "netease") != "netease":
        return "", 0, bitrate
    async with _client(10) as client:
        resp = await client.get(
            QIJIEYA,
            params={"server": "netease", "type": "url", "id": song.id, "br": bitrate},
            follow_redirects=False,
        )
        loc = str(resp.headers.get("location") or "")
    # 无版权 / 取不到时它返回 200 且没有 Location，不是 302
    if not loc or "/404" in loc:
        return "", 0, bitrate
    return loc, 0, bitrate


async def _try_enhance(song: Song, bitrate: int = 128) -> tuple[str, int, int]:
    """网易云官方免签接口，**能给出文件大小**。

    它返回的 ``code`` 很有用：200 = 有直链、-110 = 要会员、404 = 没版权。
    但拿不到会员歌，所以只能和 injahow 配合，不能单用。
    """
    if (song.source or "netease") != "netease":
        return "", 0, 128
    async with _client(10) as client:
        resp = await client.get(
            ENHANCE_URL,
            params={"ids": f"[{song.id}]", "br": bitrate * 1000},
            headers={"Referer": REFERER},
        )
        data = resp.json()
    rows = data.get("data") or []
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    url = str(row.get("url") or "")
    if not url:
        return "", 0, 128
    return url, int(row.get("size") or 0), int(row.get("br") or 0) // 1000 or 128


async def _try_aggregate(song: Song, bitrate: int) -> tuple[str, int, int]:
    """gdstudio 聚合接口。能拿会员歌、有大小，但**不稳**（503 / 截断 / 超时），
    只当最后一路兜底。"""
    data = await _get(
        {
            "types": "url",
            "source": song.source or "netease",
            "id": song.id,
            "br": bitrate,
        }
    )
    if not isinstance(data, dict):
        return "", 0, bitrate
    return (
        str(data.get("url") or "").strip(),
        int(data.get("size") or 0),
        int(data.get("br") or bitrate),
    )


async def resolve(song: Song, bitrate: int = 128) -> Song:
    """取播放直链，拿到后在原对象上补 url / size / br 并返回它本身。

    **三路并发**，谁先给出可用地址就用谁，拿到后取消其余：

        1. api.qijieya.cn（Meting）   0.3 秒，128k 完整版，会员歌也给 —— 主力
        2. 网易云 enhance/player/url  0.3~1 秒，128k 完整版，**带准确文件大小**
        3. gdstudio 聚合              会员歌也能拿，但 1~13 秒，偶尔超时

    ②只对免费歌有效（会员歌返回 code=-110），但它会带回 ``size``，
    比①光给个地址有用 —— 谁先回就用谁，两种都正常。

    为什么并发而不是串行：串行的话免费歌也得先问完一路再问下一路，
    白白叠加等待；并发之后总耗时 = 三路里最快成功的那一个。

    关于 ``bitrate``：``api.qijieya.cn`` 真的认这个参数（br=128 → 3.9MB，
    br=320 → 9.8MB），另外两路跟随同一个值。默认给 128 是有讲究的 ——
    一首 4 分钟的歌约 4MB，刚好卡在「二进制直传 5MB」以内，能走最稳的路径；
    改成 320 会变成 10MB，只能走 URL 上传（依赖 QQ 服务器自己去下载）。
    """
    cached = _url_cache.get((song.id, bitrate))
    if cached and time.monotonic() - cached[0] < _URL_TTL:
        # 换个对象返回：调用方的 Song 是搜索时的，别让它跟缓存串味
        hit = cached[1]
        song.url, song.size, song.br = hit.url, hit.size, hit.br
        return song

    tasks = [
        asyncio.create_task(_try_qijieya(song, bitrate)),
        asyncio.create_task(_try_enhance(song, bitrate)),
        asyncio.create_task(_try_aggregate(song, bitrate)),
    ]
    url, size, br = "", 0, bitrate
    pending = set(tasks)
    try:
        while pending and not url:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    got_url, got_size, got_br = task.result()
                except Exception as e:
                    logger.debug(f"取直链一路失败（{song.name}）：{e!r}")
                    continue
                if got_url:
                    url, size, br = got_url, got_size, got_br
                    break
    finally:
        # 已经拿到地址了，剩下那几路别继续跑，省得白占连接
        for task in tasks:
            if not task.done():
                task.cancel()

    if not url:
        raise RuntimeError("取不到播放地址，多半是版权受限（网易云对这首歌不提供直链）")

    song.url, song.size, song.br = url, size, br
    _url_cache[(song.id, bitrate)] = (
        time.monotonic(),
        Song(
            id=song.id, name=song.name, artists=song.artists, album=song.album,
            source=song.source, url=song.url, size=song.size, br=song.br,
            duration=song.duration,
        ),
    )
    # 缓存别无限长，顺手清掉过期的
    if len(_url_cache) > 200:
        now = time.monotonic()
        for k in [k for k, v in _url_cache.items() if now - v[0] > _URL_TTL]:
            _url_cache.pop(k, None)
    return song


async def download(url: str, limit_bytes: int = 20 * 1048576) -> bytes | None:
    """把音频下载到内存。超过 limit 直接放弃，别把进程拖死。"""
    if not url:
        return None
    try:
        async with _client(60) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.warning(f"点歌下载失败：HTTP {resp.status_code}")
                    return None
                declared = int(resp.headers.get("content-length") or 0)
                if declared and declared > limit_bytes:
                    logger.warning(f"点歌文件过大，放弃下载：{declared / 1048576:.1f}MB")
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > limit_bytes:
                        logger.warning("点歌文件超过上限，中止下载")
                        return None
                return bytes(buf)
    except Exception as e:
        logger.warning(f"点歌下载异常：{e!r}")
        return None


__all__ = ["Song", "search", "resolve", "download"]
