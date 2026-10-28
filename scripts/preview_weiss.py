# -*- coding: utf-8 -*-
"""
WS 卡查预览脚本（开发用，不参与 bot 运行）
================================================================
改版式的时候不想每次都发到群里看效果，用这个直接出图到 previews/：

    .venv\\Scripts\\python.exe scripts\\preview_weiss.py
    .venv\\Scripts\\python.exe scripts\\preview_weiss.py WS02-E002 Yukino

不带参数就出几张有代表性的，覆盖不同版式分支：
    Character  常规角色卡（黄，效果最长）
    Climax     高潮卡（没有等级/费用/力量，走另一套数值面板）
    Event      事件卡
    多图       搜出多张时走候选列表

需要先装好依赖（pillow / httpx），首次跑要连官方卡表。
"""

import asyncio
import sys
from pathlib import Path

import nonebot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

nonebot.init()

from src.plugins.weiss import render, service, translate  # noqa: E402

OUT_DIR = PROJECT_ROOT.parent / "previews"

DEFAULT_QUERIES = [
    "WS02-E002",   # 角色卡，效果很长
    "WS02-E035",   # 高潮卡
    "Yukino",      # 卡名搜索，多结果
]


async def main(queries: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for q in queries:
        cards = await service.search(q)
        print(f"\n=== {q} → {len(cards)} 张")
        for c in cards[:6]:
            print(f"   {c.no:24} {c.name[:40]:42} {c.kind:10} {c.rarity:4} "
                  f"L{c.level} C{c.cost} P{c.power} soul={c.soul} trig={c.triggers}")

        if not cards:
            continue

        main_card, _ = service.pick(cards, q)
        distinct = {c.base_no for c in cards}
        exact = (main_card.no.lower() == q.lower()
                 or main_card.name.lower() == q.lower())

        if len(distinct) > 1 and not exact:
            png = await asyncio.to_thread(render.render_candidates, q, cards[:10])
            name = f"WS_候选_{q}.png"
        else:
            img, icons, (zh_name, zh_text) = await asyncio.gather(
                service.load_image(main_card),
                service.load_parts(main_card.triggers),
                translate.translate_card(main_card),
            )
            png = await asyncio.to_thread(
                render.render_card, main_card, img, icons, zh_name, zh_text
            )
            name = f"WS_{main_card.no.replace('/', '_')}.png"
            print(f"   出图：{main_card.no}  图={'有' if img else '无'}  图标={list(icons)}")
            print(f"   中文名：{zh_name or '（保留原名）'}")
            for ln in (zh_text or main_card.text).split("\n"):
                print(f"     {ln}")

        path = OUT_DIR / name
        path.write_bytes(png)
        print(f"   → {path}  ({len(png) // 1024} KB)")


if __name__ == "__main__":
    args = sys.argv[1:] or DEFAULT_QUERIES
    asyncio.run(main(args))
