# -*- coding: utf-8 -*-
"""
WS 卡面中文翻译
================================================================
官方英文卡表只有英文。这里用腾讯 Transmart 的公开翻译接口
（`transmart.qq.com/api/imt`，网页翻译页自己用的那个，不需要 key）
把卡名和效果翻成中文。

为什么不直接整段丢给翻译接口：
    直接翻出来的东西不能看。实测「【AUTO】」会被翻成「[]」「继续」「1」，
    「power」→「能量」、「soul」→「灵魂」、「waiting room」→「候车室」。
    WS 是有固定术语的卡牌游戏，简中官方卡面用的是「力量」「魂」「休息室」
    「计时区」，机翻全部对不上。

所以走「占位符保护」：
    1. 方括号、引号、【CONT】等标记、《作品名》、术语，全部换成 «T0» 这种
       占位符 —— 实测腾讯对这个形式原样保留，一个字符都不动
    2. 剩下的自然语言才送翻译
    3. 翻回来把占位符换回中文
    4. 再用 fixes 修补机翻的固定毛病（「您的」→「你的」、「这张牌」→「此卡」…）

    为什么占位符长这样：
        «T0» 在短句里完美保留，但长句里会被腾讯"规范化"成中文引号
        （实际见到 «T17» 变成 "T17"），那串就彻底认不回来了。
        [[T0]] 稳得多，唯一的小毛病是偶尔吞掉一个 ]，还原时放宽正则即可。

    术语表在 config/ws_terms.json，改完不用重启。

翻译结果落盘缓存在 data/weiss/trans/，按卡号存。
同一张卡只翻一次 —— 接口是免费公开的，不该无谓地反复打。
术语表改动后（hash 变化）自动重翻。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

import httpx
from nonebot.log import logger

from src.plugins.settings.config import get

from .service import Card

API = "https://transmart.qq.com/api/imt"

# 翻译逻辑的版本号。改动 protect / restore / polish 的算法时手动 +1，
# 否则老缓存会让新逻辑看不见效果（hash 里只算了术语表，代码改了它不变）。
CACHE_VERSION = 2
ROOT = Path(__file__).resolve().parents[3]
TERMS_FILE = ROOT / "config" / "ws_terms.json"
CACHE_DIR = ROOT / "data" / "weiss" / "trans"

# 术语表缓存（按文件 mtime 失效，改文件即生效，不用重启）
_terms_cache: dict = {}
_terms_mtime: float = 0.0
_terms_hash: str = ""

# 内存缓存：卡号 → (中文名, 中文效果)
_mem: dict[str, tuple[str, str]] = {}
# 正在翻的卡，防止同一张卡被并发请求重复翻
_inflight: dict[str, asyncio.Task] = {}


# ---------------- 术语表 ----------------

def _load_terms() -> dict:
    """读术语表；文件没改就用内存里的。"""
    global _terms_cache, _terms_mtime, _terms_hash
    try:
        mtime = TERMS_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _terms_cache and mtime == _terms_mtime:
        return _terms_cache

    try:
        data = json.loads(TERMS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        # 术语表坏了就退化成「只保护符号和标记」，不能整个功能挂掉
        if _terms_cache:
            logger.warning(f"WS 术语表读取失败（{exc}），继续用旧的那份")
            return _terms_cache
        logger.warning(f"WS 术语表读取失败（{exc}），按空表处理")
        data = {}

    data["_compiled"] = _compile(data)
    _terms_cache = data
    _terms_mtime = mtime
    # 版本号 + 整份术语表参与 hash：只改 fixes 也要让老缓存失效，
    # 否则修了毛病也看不见
    _terms_hash = hashlib.sha256(
        (
            f"v{CACHE_VERSION}|"
            + json.dumps(
                {k: data.get(k) for k in ("terms", "markers", "fixes")},
                sort_keys=True,
                ensure_ascii=False,
            )
        ).encode()
    ).hexdigest()[:12]
    return data


def _compile(data: dict) -> dict:
    """把术语表编译成正则与映射，避免每次匹配都重新拼。"""
    terms: dict = data.get("terms") or {}
    # 按长度降序，保证「climax area」先于「climax」被匹配
    ordered = sorted(terms.items(), key=lambda kv: -len(kv[0]))
    pattern = None
    if ordered:
        pattern = re.compile(
            r"(?<![A-Za-z])(" + "|".join(re.escape(k) for k, _ in ordered) + r")(?![A-Za-z])",
            re.I,
        )
    fixes = []
    for row in data.get("fixes") or []:
        if isinstance(row, (list, tuple)) and len(row) == 2:
            try:
                fixes.append((re.compile(row[0]), row[1]))
            except re.error as exc:
                logger.warning(f"WS 术语表 fixes 里的正则无效：{row[0]!r}（{exc}）")
    return {
        # 大小写不敏感：官方卡面上【CXCOMBO】和【CxCombo】两种写法都有
        "markers": {str(k).upper(): v for k, v in (data.get("markers") or {}).items()},
        "terms": {k.lower(): v for k, v in terms.items()},
        "pattern": pattern,
        "fixes": fixes,
    }


# ---------------- 占位符保护 / 还原 ----------------

_TAG_RE = re.compile(r"【([A-Za-z]{2,10})】")
_WORK_RE = re.compile(r"《[^》]+》")
# «Tn» —— 腾讯实测原样保留。容错：偶尔会变成 « T0 » 或吞掉 »
# 腾讯偶尔会吞掉一个 ]，或者把 [[ 拆开，所以还原时放宽
_SLOT_RE = re.compile(r"\[\s*\[\s*T(\d+)\s*\]\s*\]?", re.I)
_BRACKET_RE = re.compile(r"\[\s*\[\s*B([01])\s*\]\s*\]?", re.I)
_BR = re.compile(r"[\[\]]")


def _quote_slots(text: str, slot) -> tuple[str, int]:
    """把英文双引号换成简中的「」，并正确配对嵌套。

    卡面上的引号有两种用法，而且会嵌套：
        "Turbulent Recreation"                       ← 普通引用卡名
        "【永】 … cannot play "Backup" from hand."    ← 外层整段 + 内层卡名
    顺序交替（1开2闭3开4闭）能应付第一种，第二种会配成
    「…或」助太刀「从他们的手牌。」，整句拧掉。

    这里用「最短距离优先」配对：把所有引号两两组合按间距排序，
    间距最短的先结成一对。外层跨度大，自然排到最后才配。
    卡面上被引用的东西（卡名、能力文本）都是就近闭合的，这个假设成立。
    """
    pos = [i for i, ch in enumerate(text) if ch == '"']
    n = len(pos)
    if n == 0:
        return text, 0

    partner: dict[int, int] = {}
    used: set[int] = set()
    cands = sorted((pos[j] - pos[i], i, j) for i in range(n) for j in range(i + 1, n))
    for _, i, j in cands:
        if i in used or j in used:
            continue
        partner[i] = j
        partner[j] = i
        used.add(i)
        used.add(j)

    index_of = {p: k for k, p in enumerate(pos)}
    out: list[str] = []
    for at, ch in enumerate(text):
        if ch == '"' and at in index_of:
            k = index_of[at]
            out.append(slot("「" if partner.get(k, -1) > k else "」"))
        else:
            out.append(ch)
    return "".join(out), n


def _protect(text: str, comp: dict) -> tuple[str, list[str]]:
    """把不该被翻译的片段换成 [[Tn]]，返回（改写后文本, 编号→中文 的还原表）。"""
    table: list[str] = []

    def slot(value: str) -> str:
        table.append(value)
        return f"[[T{len(table) - 1}]]"

    # 1. 方括号必须最先保护，而且要占用独立编号前缀。
    #    cost 段「[(1) Put 1 card into your waiting room]」里的 ] 会在长句里被吞掉，
    #    导致整段括号残缺。先换成槽位，机翻就动不了它。
    #    必须一次遍历同时换，分两次 replace 会互相干扰：
    #    "[" 换成 "[[B0]]" 之后，第二步的 "]" 会把 "[[B0]]" 里的 ] 也吃掉。
    text = _BR.sub(lambda m: "[[B0]]" if m.group(0) == "[" else "[[B1]]", text)

    # 2. 引号配好对再换成「」，交给 _quote_slots（嵌套也能配对）。
    if '"' in text:
        text, _ = _quote_slots(text, slot)

    # 3. 技能标记 / 作品名 / 术语
    markers = comp["markers"]
    text = _TAG_RE.sub(
        lambda m: slot("【" + markers.get(m.group(1).strip().upper(), m.group(1).strip()) + "】"),
        text,
    )
    text = _WORK_RE.sub(lambda m: slot(m.group(0)), text)

    pattern = comp["pattern"]
    if pattern:
        zh_map = comp["terms"]
        text = pattern.sub(lambda m: slot(zh_map[m.group(0).lower()]), text)
    return text, table


def _restore(text: str, table: list[str]) -> str:
    def rep(m):
        i = int(m.group(1))
        return table[i] if i < len(table) else m.group(0)

    text = _SLOT_RE.sub(rep, text)
    # 方括号槽位单独还原，同样容忍被吞掉的 ]
    text = _BRACKET_RE.sub(lambda m: "[" if m.group(1) == "0" else "]", text)
    return text


def _polish(text: str, comp: dict) -> str:
    for pattern, repl in comp["fixes"]:
        text = pattern.sub(repl, text)
    text = re.sub(r"\s+([，。、；：！？）】」》])", r"\1", text)
    # 「《作品名》 角色」这种，右书名号后面不该留空格
    text = re.sub(r"(?<=[》」】])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=\d)\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[+\-]\d)", "", text)
    text = re.sub(r"(?<=[+\-])\s+(?=\d)", "", text)
    text = re.sub(r" +(，|。|、|；|：|！|？|）」)", r"\1", text)
    # 中英之间留一个空格，读起来才不挤（「+3000力量」这种数字的不留）
    text = re.sub(r"(?<=[a-zA-Z])(?=[\u4e00-\u9fff])", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])(?=[a-zA-Z])", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=\d)", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ---------------- 翻译接口 ----------------

def _timeout() -> float:
    try:
        return float(get("weiss.translate_timeout", 10))
    except (TypeError, ValueError):
        return 10.0


async def _call_api(texts: list[str]) -> list[str]:
    """一次问一批。返回条数与输入一致；失败抛异常。"""
    payload = {
        "header": {
            "fn": "auto_translation",
            "client_key": "browser-chrome-110.0.0-Mac OS",
            "user_key": "pandabot",
            "session": "pandabot",
        },
        "type": "plain",
        "model_category": "normal",
        "source": {"lang": "en", "text_list": texts},
        "target": {"lang": "zh"},
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": "https://transmart.qq.com/zh-CN/index",
    }
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        resp = await client.post(API, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    ret = (data.get("header") or {}).get("ret_code")
    if ret not in (None, "succ"):
        raise RuntimeError(f"翻译接口返回 {ret}")
    out = data.get("auto_translation") or []
    if len(out) != len(texts):
        raise RuntimeError(f"翻译条数不匹配：要 {len(texts)} 条，回 {len(out)} 条")
    return [str(x) for x in out]


async def _api_batch(texts: list[str]) -> list[str]:
    """接口没写单次上限，自己按 16 条一批切。

    任何一批失败就整张卡放弃翻译（抛给上层回退英文）。
    这里不能"把原文塞回去接着走" —— 那样还原出来是
    「【永】 If all of your 角色 are《Dengeki Bunko》」这种中英夹杂，
    比干脆显示英文还难读。
    """
    if not texts:
        return []
    result: list[str] = []
    for i in range(0, len(texts), 16):
        chunk = texts[i : i + 16]
        result.extend(await _call_api(chunk))
    return result


# ---------------- 缓存 ----------------

def _cache_path(base_no: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", base_no)
    return CACHE_DIR / f"{safe}.json"


def _read_cache(base_no: str) -> tuple[str, str] | None:
    hit = _mem.get(base_no)
    if hit:
        return hit
    try:
        data = json.loads(_cache_path(base_no).read_text(encoding="utf-8"))
    except Exception:
        return None
    # 术语表变了就作废，否则改了术语表老卡还是旧译名
    if data.get("terms") != _terms_hash:
        return None
    name, text = data.get("name") or "", data.get("text") or ""
    if not name and not text:
        return None
    _mem[base_no] = (name, text)
    return _mem[base_no]


def _write_cache(base_no: str, name: str, text: str) -> None:
    _mem[base_no] = (name, text)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(base_no).write_text(
            json.dumps(
                {"terms": _terms_hash, "name": name, "text": text, "ts": int(time.time())},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug(f"WS 翻译缓存写入失败：{exc}")


# ---------------- 对外 ----------------

async def translate_card(card: Card) -> tuple[str, str]:
    """返回 (中文卡名, 中文效果)。

    任何一步失败都返回原文 —— 查卡功能不能因为翻译接口罢工就不可用。
    """
    if not get("weiss.translate", True):
        return card.name, card.text

    comp = _load_terms()["_compiled"]

    base_no = card.base_no or card.no
    hit = _read_cache(base_no)
    if hit:
        return hit

    running = _inflight.get(base_no)
    if running is not None:
        # 同一张卡正在翻，等它的结果，不再打一次接口
        return await asyncio.shield(running)

    task = asyncio.ensure_future(_translate(card, comp))
    _inflight[base_no] = task
    try:
        return await asyncio.shield(task)
    finally:
        _inflight.pop(base_no, None)


async def _translate(card: Card, comp: dict) -> tuple[str, str]:
    want_name = bool(get("weiss.translate_name", True))

    # 效果按行拆开：整段送翻会吃掉换行，【永】【自】的层次就没了
    lines = card.text.split("\n") if card.text else []
    prepared: list[str] = []
    tables: list[list[str]] = []

    if want_name and card.name:
        p, t = _protect(card.name, comp)
        prepared.append(p)
        tables.append(t)
    for line in lines:
        if not line.strip():
            prepared.append("")
            tables.append([])
            continue
        p, t = _protect(line, comp)
        prepared.append(p)
        tables.append(t)

    try:
        outs = await asyncio.wait_for(_api_batch(prepared), timeout=_timeout() + 4)
    except Exception as exc:
        logger.warning(f"WS 翻译整体失败，回退英文：{exc}")
        return card.name, card.text

    idx = 0
    name_zh = card.name
    if want_name and card.name:
        name_zh = _polish(_restore(outs[0], tables[0]), comp)
        idx = 1

    text_lines: list[str] = []
    for line in lines:
        if not line.strip():
            text_lines.append("")
            idx += 1
            continue
        translated = _polish(_restore(outs[idx], tables[idx]), comp)
        # 翻出空串或者只剩标点，就退回英文那一行，至少信息不丢
        text_lines.append(translated if translated.strip(" ·•-—「」") else line)
        idx += 1

    text_zh = "\n".join(text_lines) if card.text else ""

    # 卡名翻出来和原文一样（接口没动它），就当没翻，省得界面上重复两行
    if name_zh.strip() == card.name.strip():
        name_zh = ""

    _write_cache(card.base_no or card.no, name_zh, text_zh)
    return name_zh, text_zh
