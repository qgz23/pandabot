# -*- coding: utf-8 -*-
"""
WS（Weiß Schwarz / 黑白双翼）卡片数据
================================================================
数据源：官方英文卡表 https://en.ws-tcg.com/cardlist/

为什么不用中文查卡器（2026-10 实测）：
    tcgtool.cn        国内站，出口直接连不上（curl 返回 000）
    bottleneko.app    猫罐子，接口存在但没有公开文档，页面是 Nuxt SPA，
                      要靠翻 bundle 猜端点，而且响应经常 20 秒以上
    moetcg.club       萌卡社，纯前端渲染，HTML 里一行卡数据都没有
官方英文站是唯一「稳定 + 字段全 + 带卡图」的源：一页 15 张卡、1.5 秒响应、
返回规整的表单式 HTML，卡号/等级/费用/力量/魂/触发/效果/图片全都在。

卡号是全球通用的（Gaw/WS02-E002 在日文、简中、英文卡上完全一样），
所以按卡号查没有任何歧义；只有**卡名是英文的**，中文名需要另找源。

两个必须知道的坑：

1. 请求必须带 User-Agent。
   不带的话 CloudFront 直接回 404 而不是 403 —— 看起来像"网址写错了"，
   很容易照着路径排查半天。实测同一路径带 UA 立刻 200。

2. 按卡号查要用 `keyword_type[]=no`，不是 `cardno` 参数。
   英文站搜索表单里根本没有 cardno；硬拼 `?cardno=Gaw/WS02-E002`
   这个参数会被**静默忽略**，然后返回全库第一页（一堆完全无关的卡），
   而不是报错或空结果 —— 不比对卡号根本发现不了。
"""

from __future__ import annotations

import html as html_mod
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import httpx
from nonebot.log import logger

from src.plugins.settings.config import get

BASE = "https://en.ws-tcg.com"
SEARCH_URL = f"{BASE}/cardlist/searchresults/"
ICON_URL = BASE + "/cardlist/partimages/{name}.gif"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------- HTML 解析 ----------------
# 每张卡是 <li><a href="/cardlist/?cardno=...">…</a></li>，
# a 标签里依次是 图片块 / 文字块 / "View more" 按钮。
_BLOCK_RE = re.compile(
    r'<li>\s*<a href="(/cardlist/\?cardno=[^"]*)">(.*?)</a>\s*</li>', re.S
)
_NUM_RE = re.compile(r'<p class="number">([^<]*)</p>')
_NAME_RE = re.compile(r'<p class="ttl[^"]*">([^<]*)</p>')
_IMG_RE = re.compile(r'<div class="image"><img src="([^"]+)"')
_DD_RE = re.compile(r"<dt>([^<]*)</dt>\s*<dd>(.*?)</dd>", re.S)
_TEXT_RE = re.compile(r'<div class="p-cards__detail[^>]*>\s*<p>(.*?)</p>', re.S)

# 图标路径有两种前缀，都要认：
#   Side / Trigger  → /cardlist/partimages/w.gif
#   Color / Soul    → /wordpress/wp-content/images/partimages/yellow.gif
_PART_RE = re.compile(r"/partimages/([a-z0-9_]+)\.g(?:if|png)")

# 卡号长这样：Gaw/WS02-E002、SBY/W136-E001、WS02-E002（省略系列前缀）
_CARDNO_RE = re.compile(r"^[A-Za-z0-9]{2,8}(?:/[A-Za-z0-9]{1,8})?-[A-Za-z]{1,3}\d{2,}")

# 「没有值」的几种写法
_EMPTY = {"", "－", "-", "—", "n/a"}


def _clean(raw: str) -> str:
    """HTML 片段转纯文本：<br> 变换行，去标签，还原实体。"""
    s = re.sub(r"<br\s*/?>", "\n", raw)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_mod.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


@dataclass
class Card:
    no: str = ""                 # Gaw/WS02-E002
    name: str = ""               # 《Nega Nebulus》 Kuroyukihime
    image: str = ""              # 卡图相对路径
    kind: str = ""               # Character / Event / Climax
    rarity: str = ""             # RR / SP / CC …
    colors: list[str] = field(default_factory=list)    # yellow / green / red / blue
    level: str = ""
    cost: str = ""
    power: str = ""
    triggers: list[str] = field(default_factory=list)  # soul / treasure / comeback …
    soul: int = 0
    text: str = ""

    @property
    def image_url(self) -> str:
        if not self.image:
            return ""
        return self.image if self.image.startswith("http") else BASE + self.image

    @property
    def is_climax(self) -> bool:
        return self.kind.lower().startswith("climax")

    @property
    def is_event(self) -> bool:
        return self.kind.lower().startswith("event")

    @property
    def base_no(self) -> str:
        """去掉稀有度后缀的卡号，用来把同名不同闪的版本归成一组。

        E002 / E002S / E002SP / E002SSP → E002
        （只砍末尾连续的大写字母，前面的 -E 是卡位编号不能动）
        """
        return re.sub(r"(?<=[0-9])[A-Z]+$", "", self.no)

    @property
    def variant(self) -> str:
        """稀有度后缀：E002SP 的 SP、E002 的空串。"""
        m = re.search(r"(?<=[0-9])([A-Z]+)$", self.no)
        return m.group(1) if m else ""


def parse_cards(page: str) -> list[Card]:
    out: list[Card] = []
    for _href, body in _BLOCK_RE.findall(page):
        m = _NUM_RE.search(body)
        if not m:
            continue
        card = Card(no=_clean(m.group(1)))
        if not card.no:
            continue

        m = _NAME_RE.search(body)
        card.name = _clean(m.group(1)) if m else ""

        m = _IMG_RE.search(body)
        card.image = m.group(1) if m else ""

        dd = {k.strip(): v for k, v in _DD_RE.findall(body)}
        card.kind = _clean(dd.get("Card Type", ""))
        card.rarity = _clean(dd.get("Rarity", ""))
        card.level = _clean(dd.get("Level", ""))
        card.cost = _clean(dd.get("Cost", ""))
        card.power = _clean(dd.get("Power", ""))
        card.colors = _PART_RE.findall(dd.get("Color", ""))

        # 触发：高潮卡和部分角色卡是图标，其余是「－」
        parts = _PART_RE.findall(dd.get("Trigger", ""))
        if parts:
            card.triggers = parts
        else:
            raw = _clean(dd.get("Trigger", ""))
            card.triggers = [] if raw.lower() in _EMPTY else [raw]

        # 魂：一个 soul 图标算 1 点，soul2 这种算 2 点
        soul_parts = _PART_RE.findall(dd.get("Soul", ""))
        if soul_parts:
            card.soul = sum(2 if p.endswith("2") else 1 for p in soul_parts)
        else:
            raw = _clean(dd.get("Soul", ""))
            card.soul = int(raw) if raw.isdigit() else 0

        m = _TEXT_RE.search(body)
        card.text = _clean(m.group(1)) if m else ""
        out.append(card)
    return out


# ---------------- 缓存 ----------------
# 搜索结果：同一个人连查两次很常见（发错了再发一次），缓存能省一次网络往返
_search_cache: "OrderedDict[str, tuple[float, list[Card]]]" = OrderedDict()
_SEARCH_CACHE_MAX = 128
_CACHE_TTL = 600

# 卡图：单张 340 KB，48 张约 16 MB。用 LRU 而不是无限长，
# 群里连查几十张冷门卡不至于把内存吃满
_img_cache: "OrderedDict[str, bytes]" = OrderedDict()
_IMG_CACHE_MAX = 48

# 触发/颜色小图标：几十字节一个，可以一直留着
_part_cache: dict[str, bytes] = {}


def _timeout() -> float:
    try:
        return float(get("weiss.request_timeout", 25))
    except (TypeError, ValueError):
        return 25.0


async def _get_text(params: dict[str, str]) -> str:
    async with httpx.AsyncClient(
        headers=_HEADERS, timeout=_timeout(), follow_redirects=True
    ) as client:
        r = await client.get(SEARCH_URL, params=params)
        r.raise_for_status()
        return r.text


async def _search_raw(keyword: str, kind: str) -> list[Card]:
    key = f"{kind}\x00{keyword.lower()}"
    now = time.time()
    hit = _search_cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        _search_cache.move_to_end(key)
        return hit[1]

    # httpx 会把 list 值展开成重复的 keyword=…&keyword_type[]=…
    page = await _get_text({"keyword": keyword, "keyword_type[]": [kind], "view": "text"})
    cards = parse_cards(page)

    _search_cache[key] = (now, cards)
    _search_cache.move_to_end(key)
    while len(_search_cache) > _SEARCH_CACHE_MAX:
        _search_cache.popitem(last=False)
    return cards


def _rank(cards: list[Card], query: str) -> list[Card]:
    """同一张卡的不同闪版排在一起，基础版在前。

    官方返回的顺序是「基础版、S 版、SP 版……」这种成对排列，
    但不同系列不一定守规矩，所以自己按卡号长度（=后缀长度）排一次。
    """
    return sorted(cards, key=lambda c: (len(c.no), c.no))


async def search(query: str) -> list[Card]:
    """按卡号或卡名搜卡。

    卡号走 keyword_type=no（精确），卡名走 name，都不中就放宽到全字段
    （用户可能输入的是特征名，比如《Fate》）。
    """
    q = query.strip()
    if not q:
        return []

    if _CARDNO_RE.match(q):
        cards = await _search_raw(q, "no")
        if cards:
            return _rank(cards, q)

    cards = await _search_raw(q, "name")
    if cards:
        return cards

    cards = await _search_raw(q, "all")
    return cards


def pick(cards: list[Card], query: str) -> tuple[Card | None, list[Card]]:
    """从搜索结果挑出要出图的那一张，返回 (主卡, 其他版本)。

    优先完全相同的卡号，其次「卡号以 /查询词 结尾」（用户省略了系列前缀），
    再次卡名完全相等，最后退回第一张。
    """
    if not cards:
        return None, []
    q = query.strip().lower()

    main = None
    for c in cards:
        if c.no.lower() == q:
            main = c
            break
    if main is None:
        tail = [c for c in cards if c.no.lower().endswith("/" + q)]
        if tail:
            main = _rank(tail, query)[0]
    if main is None:
        exact = [c for c in cards if c.name.lower() == q]
        if exact:
            main = exact[0]
    if main is None:
        main = cards[0]

    others = [c for c in cards if c is not main]
    return main, others


async def _get_bytes(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, timeout=_timeout(), follow_redirects=True
        ) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            return r.content
    except Exception:
        logger.debug(f"WS 资源下载失败：{url}")
        return None


async def load_image(card: Card) -> bytes | None:
    """卡图，带 LRU 内存缓存。"""
    url = card.image_url
    if not url:
        return None
    if url in _img_cache:
        _img_cache.move_to_end(url)
        return _img_cache[url]

    raw = await _get_bytes(url)
    if raw:
        _img_cache[url] = raw
        while len(_img_cache) > _IMG_CACHE_MAX:
            _img_cache.popitem(last=False)
    return raw


async def load_part(name: str) -> bytes | None:
    """触发 / 颜色的小图标（gif，几十字节）。"""
    if name in _part_cache:
        return _part_cache[name]
    # 触发字段偶尔是纯文字（「－」之类），不是图标名，别白跑一次 404
    if not re.fullmatch(r"[a-z0-9_]+", name):
        return None
    raw = await _get_bytes(ICON_URL.format(name=name))
    if raw:
        _part_cache[name] = raw
    return raw


async def load_parts(names: list[str]) -> dict[str, bytes]:
    """批量取图标。并发发出去，任何一个失败都不影响其他的。"""
    if not names:
        return {}
    import asyncio

    results = await asyncio.gather(
        *(load_part(n) for n in names), return_exceptions=True
    )
    return {
        n: r
        for n, r in zip(names, results)
        if isinstance(r, (bytes, bytearray)) and r
    }
