# -*- coding: utf-8 -*-
"""
从 PRTS wiki 抓子职业（分支）中文名
================================================================
gamedata 里 `subProfessionId` 只有英文（siegesniper / splashcaster），
中文名在游戏 UI 文本里，鹰角没放进数据包。网上流传的映射表都是手抄的，
抄错一个你也看不出来。

所以这里走权威源：PRTS 的干员页面模板里有 `|职业=` 和 `|分支=` 两个字段，
一个干员一条，抓下来反查就是完整的 subProfessionId → 中文名。

做法：
    1. 每个子职业挑 2~3 个代表干员（优先星级高、名字短的，避开「预备干员-」）
    2. 用 MediaWiki 的 query API 一次拿 50 个页面的 wikitext（一个请求顶 50 次）
    3. 正则抠 `|分支=xxx`，填进映射
    4. 没抓到的子职业留在日志里，人工补

输出 src/plugins/arknights/subprof_names.json，这个文件随仓库提交。

    .venv\\Scripts\\python.exe scripts\\fetch_subprof_names.py
"""

import asyncio
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import httpx
import nonebot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

nonebot.init()

from src.plugins.arknights.service import _fetch_json  # noqa: E402

OUT_PATH = PROJECT_ROOT / "src" / "plugins" / "arknights" / "subprof_names.json"

PRTS_API = "https://prts.wiki/api.php"
BATCH = 50          # MediaWiki titles 参数上限
UA = {"User-Agent": "pandabot/1.0 (arknights codex)"}

# 页面里两种写法都见过：|分支=领主 和 |分支 = 领主
_BRANCH_RE = re.compile(r"\|\s*分支\s*=\s*([^|\n}]+)")
_PROF_RE = re.compile(r"\|\s*职业\s*=\s*([^|\n}]+)")

# 这些是教学用的占位干员，PRTS 上未必有独立页面
_BAD_NAMES = ("预备干员", "char_", "陷阱", "装置")


def pick_representatives(chars_raw: dict) -> dict[str, list[str]]:
    """每个子职业挑几个代表干员，用来去 PRTS 上问「你的分支是什么」。"""
    pool: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for raw in chars_raw.values():
        if not isinstance(raw, dict):
            continue
        sub = raw.get("subProfessionId")
        name = (raw.get("name") or "").strip()
        if not sub or not name or any(bad in name for bad in _BAD_NAMES):
            continue
        rarity = int(str(raw.get("rarity", "TIER_1")).split("_")[-1] or 1)
        pool[sub].append((rarity, name))

    out: dict[str, list[str]] = {}
    for sub, items in pool.items():
        # 星级高的先试（页面信息更全），同星级按名字长度（越短越不容易撞消歧义）
        items.sort(key=lambda t: (-t[0], len(t[1])))
        # 名字里带括号/间隔号的多半是异格或特殊干员，排在后面
        items.sort(key=lambda t: any(ch in t[1] for ch in "()（）·"))
        out[sub] = [name for _r, name in items[:3]]
    return out


async def fetch_pages(titles: list[str], client: httpx.AsyncClient) -> dict[str, str]:
    """批量取 wikitext，返回 {页面名: 正文}。"""
    result: dict[str, str] = {}
    for i in range(0, len(titles), BATCH):
        chunk = titles[i : i + BATCH]
        try:
            resp = await client.get(
                PRTS_API,
                params={
                    "action": "query",
                    "prop": "revisions",
                    "rvslots": "main",
                    "rvprop": "content",
                    "titles": "|".join(chunk),
                    "format": "json",
                    "formatversion": "2",
                    "redirects": "1",
                },
            )
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", [])
        except Exception as e:
            print(f"  批次 {i // BATCH + 1} 请求失败：{type(e).__name__}")
            continue

        for page in pages:
            if page.get("missing"):
                continue
            revisions = page.get("revisions") or []
            if not revisions:
                continue
            content = (revisions[0].get("slots", {}).get("main", {}) or {}).get("content") or ""
            # 重定向后的标题可能变了，两个都记
            result[page.get("title", "")] = content
        await asyncio.sleep(0.4)  # 别把 wiki 打疼
    return result


def extract_branch(content: str) -> tuple[str, str]:
    """从干员页正文抠 (职业, 分支)。"""
    branch = _BRANCH_RE.search(content)
    prof = _PROF_RE.search(content)
    return (
        prof.group(1).strip() if prof else "",
        branch.group(1).strip() if branch else "",
    )


async def main() -> None:
    print("拉取 gamedata …")
    chars_raw = await _fetch_json("character_table")
    reps = pick_representatives(chars_raw)
    print(f"子职业 {len(reps)} 个，准备查询 {sum(len(v) for v in reps.values())} 个干员页面")

    mapping: dict[str, str] = {}
    prof_of: dict[str, str] = {}
    unresolved: list[str] = []

    async with httpx.AsyncClient(timeout=40, headers=UA, follow_redirects=True) as client:
        for round_no in range(3):
            # 每轮只查还没解决的子职业的第 round_no 个代表
            wanted: dict[str, str] = {}   # 干员名 -> 子职业 id
            for sub, names in reps.items():
                if sub in mapping or round_no >= len(names):
                    continue
                wanted[names[round_no]] = sub
            if not wanted:
                break

            print(f"第 {round_no + 1} 轮：查 {len(wanted)} 个页面")
            pages = await fetch_pages(list(wanted), client)

            for title, content in pages.items():
                prof, branch = extract_branch(content)
                if not branch:
                    continue
                # 页面标题可能被重定向改写，按名字反查原始请求
                sub = wanted.get(title)
                if sub is None:
                    # 找不到就用「包含关系」兜一下
                    for name, s in wanted.items():
                        if name in title or title in name:
                            sub = s
                            break
                if sub and sub not in mapping:
                    mapping[sub] = branch
                    prof_of[sub] = prof

    unresolved = sorted(set(reps) - set(mapping))
    print(f"\n解析成功 {len(mapping)} / {len(reps)}")
    if unresolved:
        print("未解析：", unresolved)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            dict(sorted(mapping.items())),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {OUT_PATH}")

    # 顺便打印一份对照表，肉眼扫一遍有没有明显错的
    print("\n对照表：")
    for sub, cn in sorted(mapping.items()):
        print(f"  {sub:20} {cn}")


if __name__ == "__main__":
    asyncio.run(main())
