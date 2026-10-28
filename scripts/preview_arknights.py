# -*- coding: utf-8 -*-
"""
方舟图鉴预览脚本（开发用，不参与 bot 运行）
================================================================
改版式的时候不想每次都发到群里看效果，用这个直接出图到 previews/：

    .venv\\Scripts\\python.exe scripts\\preview_arknights.py
    .venv\\Scripts\\python.exe scripts\\preview_arknights.py 玛恩纳 缄默德克萨斯

不带参数就出几张有代表性的：
    阿米娅    5 星，单天赋
    银灰      6 星，双天赋
    克洛丝    3 星，低星无精英
    Miss.Christine  名字最长
    预备干员-后勤    无天赋无技能（测版式兜底）

需要先装好依赖（pillow / httpx），首次跑会下载 25 MB 数据，
之后读 data/arknights/chars.json，秒出。
"""

import asyncio
import sys
from pathlib import Path

import nonebot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

nonebot.init()

from src.plugins.arknights import render, service  # noqa: E402

OUT_DIR = PROJECT_ROOT.parent / "previews"

# 默认抽样：覆盖「有天赋/无天赋」「高星/低星」「长名字」几种版式分支
DEFAULT_QUERIES = ["阿米娅", "银灰", "克洛丝", "史尔特尔", "Miss.Christine"]


async def main(queries: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not await service.index.ensure():
        print("数据没准备好，检查网络")
        return
    print(f"库内 {service.index.count} 名干员")

    for query in queries:
        hits = service.index.search(query, limit=1)
        if not hits:
            print(f"  跳过 {query}：没搜到")
            continue
        char = hits[0]
        portrait = await service.load_portrait(char.id)
        png = render.render_card(char, portrait)
        path = OUT_DIR / f"方舟_{char.name}.png"
        path.write_bytes(png)
        print(
            f"  {char.name:<14} {char.rarity}星 {len(png) // 1024:>3}KB "
            f"立绘={'有' if portrait else '无'} → {path.name}"
        )

    # 候选列表版式
    hits = service.index.search("德", limit=6)
    (OUT_DIR / "方舟_候选列表.png").write_bytes(render.render_candidates("德", hits))
    print(f"  候选列表 {[h.name for h in hits]}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or DEFAULT_QUERIES))
