# -*- coding: utf-8 -*-
"""
明日方舟干员数据
================================================================
数据源：Kengxxiao/ArknightsGameData（社区维护的官方数据包提取）
    角色表  zh_CN/gamedata/excel/character_table.json   ~14 MB
    技能表  zh_CN/gamedata/excel/skill_table.json       ~11 MB

**为什么不每次查都去拉**：两个文件加起来 25 MB，查一次下载 25 MB 是自杀行为，
而且 JSON 解析本身就要 1~2 秒。所以流程是：

    首次（或缓存过期）   下载 → 精简 → 写 data/arknights/chars.json（约 600 KB）
    之后每次查询         只读这个 600 KB 的精简库，毫秒级

精简库里存的都是"已经翻译好的、卡片上要显示的东西"，原始字段一个不留。
原始表里 90% 的内容是技能升级材料、黑board 数值、特效 prefab 路径，
图鉴一个都用不上。

数据源有两个域名，按顺序试：
    jsdelivr   国内能通，走 CDN，实测 3 秒下完 14 MB
    raw.github 兜底，需要代理
任何一个成功就停，全失败就退回旧缓存（有旧缓存就继续用，没有就报错）。

立绘单独走 Aceship/Arknight-Images：1024×1024 透明 PNG，约 300 KB/张，
按干员 id 单独下载并落盘缓存，查过谁就存谁，不批量下载。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from nonebot.log import logger

from src.plugins.settings.config import get

from .mapping import PLAYABLE_PROFESSIONS, faction_of, rarity_of

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data" / "arknights"
INDEX_PATH = DATA_DIR / "chars.json"
IMG_DIR = DATA_DIR / "img"

# 精简库结构版本。字段一改就要 +1，否则老缓存配上新代码会读出缺字段的
# 干员对象（比如 skills 从「字符串列表」变成「字典列表」），
# 渲染时直接 TypeError，而且报错位置在很远的 render.py，很难查。
INDEX_VERSION = 6

# 数据源。{name} 由调用方填 character_table / skill_table
_GAMEDATA_MIRRORS = (
    "https://cdn.jsdelivr.net/gh/Kengxxiao/ArknightsGameData@master/zh_CN/gamedata/excel/{name}.json",
    "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/master/zh_CN/gamedata/excel/{name}.json",
    "https://gcore.jsdelivr.net/gh/Kengxxiao/ArknightsGameData@master/zh_CN/gamedata/excel/{name}.json",
)

# 立绘。_1 是精一立绘（约 300 KB），_2 精二（能到 2.7 MB，不划算，不用）
# 三个 CDN 依次试：这张图单张 300 KB~500 KB，慢的时候要 4 秒，多一个源多一次机会
_LIVE_MIRRORS = (
    "https://cdn.jsdelivr.net/gh/Aceship/Arknight-Images@main/characters/{id}_1.png",
    "https://gcore.jsdelivr.net/gh/Aceship/Arknight-Images@main/characters/{id}_1.png",
    "https://raw.githubusercontent.com/Aceship/Arknight-Images/main/characters/{id}_1.png",
)

# character_table 里的富文本标记，形如「攻击造成<@ba.kw>法术伤害</>」
# 只删标签本身，"法术伤害"要留下
_TAG_RE = re.compile(r"<[^<>]*>")

_PHASE_ORDER = {"PHASE_0": 0, "PHASE_1": 1, "PHASE_2": 2, "PHASE_3": 3}


@dataclass
class Char:
    """一个干员在图鉴里要用到的全部信息。字段名故意短，600 KB 的库要省着存。"""

    id: str
    name: str
    en: str = ""
    rarity: int = 1
    prof: str = ""
    sub: str = ""
    faction: str = ""
    position: str = ""
    tags: list[str] = field(default_factory=list)
    trait: str = ""          # 特性：这个子职业干什么的
    usage: str = ""          # 一句话介绍（"罗德岛公开领导人…"）
    obtain: str = ""         # 获取途径
    stats: dict[str, Any] = field(default_factory=dict)
    talents: list[dict] = field(default_factory=list)   # [{name, desc, lv}]
    skills: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "n": self.name, "en": self.en, "r": self.rarity,
            "p": self.prof, "s": self.sub, "f": self.faction, "pos": self.position,
            "t": self.tags, "tr": self.trait, "u": self.usage, "o": self.obtain,
            "st": self.stats, "ta": self.talents, "sk": self.skills,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Char":
        return cls(
            id=d["id"], name=d["n"], en=d.get("en", ""), rarity=d.get("r", 1),
            prof=d.get("p", ""), sub=d.get("s", ""), faction=d.get("f", ""),
            position=d.get("pos", ""), tags=d.get("t") or [], trait=d.get("tr", ""),
            usage=d.get("u", ""), obtain=d.get("o", ""), stats=d.get("st") or {},
            talents=d.get("ta") or [], skills=d.get("sk") or [],
        )


def clean(text: str) -> str:
    """去掉富文本标签，顺便把连续空白压成一个空格。"""
    if not text:
        return ""
    # 技能描述里的换行是**字面的反斜杠 + n**（`\\n` 两个字符），不是真换行，
    # 所以 \s+ 压不掉它。不单独处理的话，卡片上会直接印出 "\n" 两个字符。
    text = text.replace("\\n", " ")
    return re.sub(r"\s+", " ", _TAG_RE.sub("", text)).strip()


def normalize(text: str) -> str:
    """搜索用的归一化：全角转半角（ＮＦＫＣ）、去空格、转小写。

    不做这一步的话，用户用全角输入法打出「阿米娅」后面跟个全角空格就搜不到。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


# --------------------------------------------------------------------------
# 原始数据 → 精简库
# --------------------------------------------------------------------------

def _pick_trait(raw: dict) -> str:
    """特性文本。

    新版数据在 trait.candidates[].description，老干员只有顶层 description。
    两种都要兼容 —— 只认一种的话，一半干员会显示空特性。
    """
    trait = raw.get("trait")
    if isinstance(trait, dict):
        cands = trait.get("candidates") or []
        if cands:
            text = clean(cands[-1].get("description", ""))
            if text:
                return text
    return clean(raw.get("description", ""))


def _pick_talents(raw: dict) -> list[dict]:
    """天赋。

    talents 是「天赋组」的列表，每组里有多个 candidates：
    同一个天赋在不同精英阶段/潜能下的不同版本，名字一样、数值递增。
    这里每组只留**最高阶的那个版本**（阶段高 + 潜能高），
    否则一张卡上会出现三遍「情绪吸收」。
    """
    result: list[dict] = []
    for group in raw.get("talents") or []:
        best = None
        best_key = (-1, -1)
        for cand in group.get("candidates") or []:
            name = clean(cand.get("name", ""))
            if not name or name == "？？？":
                continue
            cond = cand.get("unlockCondition") or {}
            key = (_PHASE_ORDER.get(cond.get("phase", "PHASE_0"), 0),
                   cand.get("requiredPotentialRank", 0))
            if key > best_key:
                best, best_key = cand, key
        if not best:
            continue
        desc = clean(best.get("description", ""))
        if not desc:
            continue
        # 潜能加成写成「（+2%）」「（+0.5）」挂在主值后面。
        # 图鉴不显示潜能，留着只会让人以为是基础值的一部分。
        # 括号里一定以 + 开头，所以按这个删不会误伤正常括号。
        desc = re.sub(r"（\+[^）]*）", "", desc).strip()
        result.append({
            "name": clean(best.get("name", "")),
            "desc": desc,
            "lv": best_key[0],  # 0=初始 1=精一 2=精二
        })
    return result


# 技能描述里的数值占位符：{key} / {key:0%} / {key:0.0} / {key:0.00}
_PLACEHOLDER_RE = re.compile(r"\{([^{}:]+)(?::([^{}]+))?\}")


def _format_number(value: float, fmt: str) -> str:
    """按方舟的格式串渲染数值。fmt 是 "0%" / "0.0%" / "0.0" / "0" 这种写法。"""
    if "%" in fmt:
        decimals = len(fmt.split(".", 1)[1].rstrip("%")[:3]) if "." in fmt else 0
        return f"{value * 100:.{decimals}f}%"
    decimals = len(fmt.split(".", 1)[1][:3]) if "." in fmt else 0
    if decimals:
        return f"{value:.{decimals}f}"
    return f"{value:g}"  # 2.0 -> "2"，不留下 ".0"


def format_skill_desc(level: dict) -> str:
    """把技能描述里的 {占位符} 换成真实数值。

    真实数值在同级 level 的 blackboard 里（[{key, value}]）。
    找不到对应 key 时直接删掉占位符 —— 宁可少一句话，
    也不能把 `{sp}` 这种花括号原样印到卡片上。

    **key 大小写要对齐**：描述里写的是 `{HP_RECOVERY_PER_SEC_BY_MAX_HP_RATIO:0.0%}`，
    blackboard 里却是 `hp_recovery_per_sec_by_max_hp_ratio`。同一份数据里
    大小写都不统一，所以整张表按小写建，查的时候也转小写。
    不做这一步，银灰「雪境生存法则」的回复量会直接消失。
    """
    desc = level.get("description") or ""
    if not desc:
        return ""
    # 先剥标签再处理换行：原文是「…切换：</>\n攻击范围缩小」，
    # 带着 </> 的时候「：」和 \n 并不相邻，标点判断会失效，
    # 结果是冒号后面多出一个逗号（「切换：， 攻击范围缩小」）。
    desc = _TAG_RE.sub("", desc)
    # 原文用 \n 分段。直接压成空格会读成「攻击范围内的目标 技能自动开启」，
    # 两句话粘在一起；所以前面已经有标点的就只留空格，其余补个逗号。
    desc = re.sub(r"(?<=[。！？；，、：])\\n", " ", desc)
    desc = desc.replace("\\n", "，")
    board = {
        b.get("key", "").lower(): b.get("value")
        for b in (level.get("blackboard") or [])
        if isinstance(b, dict) and b.get("key")
    }

    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        fmt = (match.group(2) or "").strip()
        # 占位符的 key 可以带一元负号：真银斩写的是 `{-def:0%}`，
        # 意思是「把 def 取反再显示」。不认这个负号，整个数值会消失，
        # 卡片上就剩一句「防御力-，」。
        negate = key.startswith("-")
        if negate:
            key = key[1:]
        if key.lower() not in board:
            return ""
        try:
            value = float(board[key.lower()])
        except (TypeError, ValueError):
            return str(board[key.lower()])
        if negate:
            value = -value
        return _format_number(value, fmt)

    return clean(_PLACEHOLDER_RE.sub(repl, desc))


def _pick_skills(raw: dict, skill_table: dict) -> list[dict]:
    """技能：名字 + 效果描述 + 图标 id。

    描述里有一堆数字占位符，形如 `{attack_speed}` `{def:0%}`，
    真实数值在同级的 blackboard 里（键值对）。不替换的话卡片上会印出花括号。

    数值取**专精三**（levels 最后一个）：图鉴是给人看上限的，
    1 级描述除了数值小没别的信息，而专精三还多了定性说明。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for sk in raw.get("skills") or []:
        sid = sk.get("skillId") or ""
        entry = skill_table.get(sid)
        if not isinstance(entry, dict) or entry.get("hidden"):
            continue
        levels = entry.get("levels") or []
        if not levels:
            continue
        name = clean(levels[0].get("name", ""))
        # 异格干员会和本体共用技能，同名只显示一次
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({
            "n": name,
            "d": format_skill_desc(levels[-1]),
            "i": sid,   # 图标 id，渲染时拼 URL 用
        })
    return out


def _pick_stats(raw: dict) -> dict[str, Any]:
    """满级属性。

    取最后一次精英化的最后一个等级帧 = 精二满级（没精二的取当前最高）。
    """
    phases = raw.get("phases") or []
    if not phases:
        return {}
    last = phases[-1]
    frames = last.get("attributesKeyFrames") or []
    if not frames:
        return {}
    data = frames[-1].get("data") or {}
    return {
        "hp": int(data.get("maxHp", 0)),
        "atk": int(data.get("atk", 0)),
        "def": int(data.get("def", 0)),
        "res": int(data.get("magicResistance", 0)),
        "cost": int(data.get("cost", 0)),
        "block": int(data.get("blockCnt", 0)),
        "interval": round(float(data.get("baseAttackTime", 0)), 1),
        "level": int(frames[-1].get("level", 0)),
        "elite": len(phases) - 1,
    }


def build_index(chars_raw: dict, skill_table: dict) -> list[dict]:
    """把两张原始表压成一张精简表。"""
    out: list[dict] = []
    for cid, raw in chars_raw.items():
        if not isinstance(raw, dict):
            continue
        # 装置/召唤物/剧情 NPC 全部排除，只留八大职业
        if raw.get("profession") not in PLAYABLE_PROFESSIONS:
            continue
        name = clean(raw.get("name", ""))
        if not name:
            continue
        char = Char(
            id=cid,
            name=name,
            en=clean(raw.get("appellation", "")),
            rarity=rarity_of(raw.get("rarity", "")),
            prof=raw.get("profession", ""),
            sub=raw.get("subProfessionId", ""),
            faction=faction_of(raw.get("nationId"), raw.get("groupId")),
            position=raw.get("position", ""),
            tags=[t for t in (raw.get("tagList") or []) if t],
            trait=_pick_trait(raw),
            usage=clean(raw.get("itemUsage", "")),
            obtain=clean(raw.get("itemObtainApproach", "")),
            stats=_pick_stats(raw),
            talents=_pick_talents(raw),
            skills=_pick_skills(raw, skill_table),
        )
        out.append(char.to_dict())

    # 排序：星级高的在前，同星级按名字。搜索结果的"候选列表"顺序就靠这个
    out.sort(key=lambda d: (-d["r"], d["n"]))
    return out


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------

async def _fetch_json(name: str, timeout: float = 45.0) -> dict:
    """下载一个 gamedata 文件，多镜像依次重试。"""
    last_err: Exception | None = None
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        for url in _GAMEDATA_MIRRORS:
            target = url.format(name=name)
            try:
                started = time.monotonic()
                resp = await client.get(target, headers={"User-Agent": "pandabot/1.0"})
                resp.raise_for_status()
                data = resp.json()
                logger.info(
                    f"方舟数据 {name} 下载完成："
                    f"{len(resp.content) / 1048576:.1f} MB / {time.monotonic() - started:.1f}s"
                )
                return data
            except Exception as e:
                last_err = e
                logger.warning(f"方舟数据源失败 {target.split('/')[2]}：{type(e).__name__}")
    raise RuntimeError(f"{name} 全部数据源均失败：{last_err!r}")


async def fetch_portrait(char_id: str) -> bytes | None:
    """下载干员立绘（1024×1024 PNG）。找不到就返回 None，卡片会自动跳过配图。

    注意 404 和超时要分开对待：
        404     这个镜像没这张图 → 立刻换下一个源，别浪费时间
        超时/断连 镜像不稳 → 同一个源再试一次，很多时候第二次就通了
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        for url in _LIVE_MIRRORS:
            target = url.format(id=char_id)
            for _ in range(2):
                try:
                    resp = await client.get(target, headers={"User-Agent": "pandabot/1.0"})
                    if resp.status_code == 404:
                        break  # 换源
                    resp.raise_for_status()
                    return resp.content
                except httpx.HTTPStatusError:
                    break
                except Exception as e:
                    logger.debug(f"立绘下载重试 {char_id}：{type(e).__name__}")
    return None


# --------------------------------------------------------------------------
# 索引管理
# --------------------------------------------------------------------------

class ArknightsIndex:
    """精简库的加载者。全局只用一个实例（模块底部的 index）。"""

    def __init__(self) -> None:
        self.chars: list[Char] = []
        self._by_id: dict[str, Char] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self._updated_at: float = 0.0

    # ---------------------------------------------------------------- 加载
    @property
    def ttl_seconds(self) -> float:
        return float(get("arknights.data_ttl_hours", 168)) * 3600

    def _cache_fresh(self) -> bool:
        if not INDEX_PATH.exists():
            return False
        age = time.time() - INDEX_PATH.stat().st_mtime
        return age < self.ttl_seconds

    def _load_from_disk(self) -> bool:
        try:
            payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"方舟精简库读取失败，将重新下载：{e!r}")
            return False
        if payload.get("version") != INDEX_VERSION:
            logger.info("方舟精简库结构版本变化，重新构建")
            return False
        try:
            chars = [Char.from_dict(d) for d in payload.get("chars", [])]
        except Exception as e:
            logger.warning(f"方舟精简库内容损坏，将重新下载：{e!r}")
            return False
        if not chars:
            return False
        self._install(chars, payload.get("updated_at", 0))
        return True

    def _install(self, chars: list[Char], updated_at: float) -> None:
        self.chars = chars
        self._by_id = {c.id: c for c in chars}
        self._updated_at = updated_at
        self._loaded = True
        logger.info(f"方舟图鉴就绪：{len(chars)} 名干员")

    def _write_cache(self, chars: list[Char], updated_at: float) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": INDEX_VERSION,
                "updated_at": updated_at,
                "chars": [c.to_dict() for c in chars],
            }
            tmp = INDEX_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(INDEX_PATH)  # 原子替换，避免写到一半断电留个坏文件
        except Exception as e:
            logger.warning(f"方舟精简库写入失败（不影响本次使用）：{e!r}")

    async def refresh(self, force: bool = False) -> None:
        """确保索引可用。force=True 时无视缓存直接重新下载。"""
        async with self._lock:
            if self._loaded and not force:
                return
            if not force and self._cache_fresh() and self._load_from_disk():
                return
            if not force and self._loaded:
                return

            started = time.monotonic()
            try:
                raw = await _fetch_json("character_table")
                skill_table = await _fetch_json("skill_table")
                rows = await asyncio.to_thread(build_index, raw, skill_table)
                del raw, skill_table  # 25 MB 的原始 dict 别留着占内存
                if not rows:
                    raise RuntimeError("精简结果为空，数据格式可能变了")
                chars = [Char.from_dict(d) for d in rows]
                self._install(chars, time.time())
                self._write_cache(chars, time.time())
                logger.info(f"方舟图鉴构建完成，耗时 {time.monotonic() - started:.1f}s")
            except Exception as e:
                logger.error(f"方舟图鉴构建失败：{e!r}")
                # 有旧库就照旧用 —— 数据源挂了不该让查图鉴功能整个哑掉
                if not self._loaded and not self._load_from_disk():
                    raise

    async def ensure(self) -> bool:
        """命令入口调用。不抛异常，失败返回 False 让调用方给用户一句话。"""
        try:
            await self.refresh()
            return self._loaded
        except Exception:
            return False

    # ---------------------------------------------------------------- 查询
    def get(self, char_id: str) -> Char | None:
        return self._by_id.get(char_id)

    def search(self, query: str, limit: int = 8) -> list[Char]:
        """模糊搜索。

        优先级：完全相等 > 前缀 > 包含。英文名（appellation）也参与匹配，
        这样打 amiya 也能查出来。
        排序稳定的关键在于：同级内保持星级降序（索引已按星级排好），
        所以搜「德」时六星会排在低星前面。
        """
        q = normalize(query)
        if not q:
            return []
        exact: list[Char] = []
        prefix: list[Char] = []
        contains: list[Char] = []
        for c in self.chars:
            names = (normalize(c.name), normalize(c.en))
            if any(n == q for n in names if n):
                exact.append(c)
            elif any(n.startswith(q) for n in names if n):
                prefix.append(c)
            elif any(q in n for n in names if n):
                contains.append(c)

        # 同组内先按名字短的排。搜「德」时「德克萨斯」(4 字) 应该排在
        # 「德·托莱多」(5 字) 前面 —— 用户打了两个字，多半想要短名那个。
        # 长度相同再按星级（索引本身已按星级降序，sorted 是稳定的）。
        def near(c: Char) -> tuple[int, int]:
            return (len(c.name), -c.rarity)

        return (exact + sorted(prefix, key=near) + sorted(contains, key=near))[:limit]

    @property
    def count(self) -> int:
        return len(self.chars)

    @property
    def updated_at(self) -> float:
        """当前索引的构建时间（unix 秒）。用于防止 /方舟更新 被连点。"""
        return self._updated_at


# 立绘本地缓存：查过谁就存谁，第二次直接读盘
def portrait_path(char_id: str) -> Path:
    return IMG_DIR / f"{char_id}.png"


async def load_portrait(char_id: str) -> bytes | None:
    """取立绘字节。三级：磁盘缓存 → 网络 → 放弃。"""
    path = portrait_path(char_id)
    if path.exists() and path.stat().st_size > 1024:
        return await asyncio.to_thread(path.read_bytes)
    raw = await fetch_portrait(char_id)
    if not raw:
        return None
    try:
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, raw)
    except Exception as e:
        logger.debug(f"立绘落盘失败 {char_id}：{e!r}")
    return raw


# --------------------------------------------------------------------------
# 技能图标
# --------------------------------------------------------------------------
# 图标文件名和 skillId 不是一一对应，要试两种：
#     skchr_svrash_2         → skill_icon_skchr_svrash_2.png
#     skcom_charge_cost[1]   → skill_icon_skcom_charge_cost[1].png  （带方括号！）
# 通用技能（skcom 开头）的图标文件名里保留了 [等级] 后缀，
# 所以先试完整 id，404 再去掉 [n] 试一次。
SKILL_DIR = DATA_DIR / "skill"

_SKILL_MIRRORS = (
    "https://cdn.jsdelivr.net/gh/yuanyan3060/ArknightsGameResource@main/skill/{name}",
    "https://gcore.jsdelivr.net/gh/yuanyan3060/ArknightsGameResource@main/skill/{name}",
    "https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main/skill/{name}",
)


def _icon_candidates(skill_id: str) -> list[str]:
    """可能的图标文件名，按优先级排。"""
    names = [f"skill_icon_{skill_id}.png"]
    base = re.sub(r"\[\d+\]$", "", skill_id)
    if base != skill_id:
        names.append(f"skill_icon_{base}.png")
    return names


def skill_icon_path(skill_id: str) -> Path:
    # [ ] 在 Windows 文件名里能用，但混在路径里容易惹麻烦，统一换掉
    safe = re.sub(r"[^\w\-.@]", "_", skill_id)
    return SKILL_DIR / f"{safe}.png"


async def fetch_skill_icon(skill_id: str) -> bytes | None:
    """下载技能图标（128×128，约 15 KB）。

    图标小，但一次要拉三个，并发打同一个 CDN 时偶发断连。
    所以每个候选文件名给两次机会 —— 重试一次的收益远大于换个镜像
    （换镜像意味着重新握手，成本更高）。
    """
    from urllib.parse import quote

    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        for mirror in _SKILL_MIRRORS:
            for name in _icon_candidates(skill_id):
                url = mirror.format(name=quote(name))
                for _ in range(2):
                    try:
                        resp = await client.get(url, headers={"User-Agent": "pandabot/1.0"})
                        if resp.status_code == 404:
                            break  # 这个命名不存在，换下一个候选名
                        resp.raise_for_status()
                        return resp.content
                    except httpx.HTTPStatusError:
                        break
                    except Exception:
                        continue  # 超时/断连，同名再试一次
    return None


async def load_skill_icon(skill_id: str) -> bytes | None:
    """取技能图标字节。磁盘缓存 → 网络 → 放弃（渲染时画占位方块）。"""
    path = skill_icon_path(skill_id)
    if path.exists() and path.stat().st_size > 512:
        return await asyncio.to_thread(path.read_bytes)
    raw = await fetch_skill_icon(skill_id)
    if not raw:
        return None
    try:
        SKILL_DIR.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, raw)
    except Exception as e:
        logger.debug(f"技能图标落盘失败 {skill_id}：{e!r}")
    return raw


# 图片资源（立绘 + 技能图标）的总下载时限。超时就不等了，
# 发没图的版本 —— 一张立绘值不了 20 秒，用户早就以为 bot 卡死了
ASSET_TIMEOUT = 20.0


async def load_assets(char: Char, timeout: float = ASSET_TIMEOUT) -> tuple[bytes | None, dict[str, bytes]]:
    """并行拉立绘和技能图标，返回 (立绘, {技能 id: 图标})。

    用 asyncio.wait 而不是 gather + wait_for：超时的时候 gather 会把
    已经下好的结果一起丢掉，用户就白等了 15 秒。wait 能拿回 done 那批 ——
    立绘下好了图标没好，至少还能出一张有脸的卡。
    """
    plan: dict[asyncio.Task, tuple[str, str]] = {
        asyncio.create_task(load_portrait(char.id)): ("portrait", "")
    }
    for skill in char.skills[:3]:
        sid = skill.get("i") or ""
        if sid:
            plan[asyncio.create_task(load_skill_icon(sid))] = ("icon", sid)

    done, pending = await asyncio.wait(plan.keys(), timeout=timeout)
    for task in pending:
        task.cancel()

    portrait: bytes | None = None
    icons: dict[str, bytes] = {}
    for task in done:
        if task.cancelled() or task.exception():
            continue
        kind, sid = plan[task]
        result = task.result()
        if not result:
            continue
        if kind == "portrait":
            portrait = result
        else:
            icons[sid] = result
    return portrait, icons


index = ArknightsIndex()
