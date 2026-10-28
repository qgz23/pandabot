# -*- coding: utf-8 -*-
"""
WS（Weiß Schwarz / 黑白双翼）卡查
================================================================
    /ws WS02-E002        按卡号查（推荐，全球通用，不会歧义）
    /ws Yukino           按卡名搜，英文关键词
    /ws                  看用法

分层和方舟图鉴那套一致：
    service.py   ← 抓官方卡表、解析、缓存，不碰 nonebot 的消息类型
    render.py    ← 纯 Pillow 画卡
    __init__.py  ← 本文件：命令注册、冷却、发图

几个产品上的取舍：

1. 卡号优先。WS 同一个角色可能出几十张同名卡（不同系列、不同稀有度），
   按名字搜几乎必然搜出一堆。卡号是全球唯一标识，最可靠。
   所以帮助里第一句就是「/ws 卡号」。

2. 搜出多张不同的卡时先出候选表，不猜用户要哪张。
   但「同一张卡的不同闪版」（E002 / E002S / E002SP）算一张，
   直接出基础版 —— 用户查的是卡号，不是想要 SP 的图。

3. 卡图下载失败也照常出卡。查卡的价值在数据，
   官方图挂了不该让整个功能不可用。
"""

import asyncio
import time

from nonebot import on_command
from nonebot.adapters.qq import Message, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from src.plugins.settings.config import get

from .render import render_card, render_candidates
from .service import Card, load_image, load_parts, pick, search

# 每人上次查询时间（monotonic 秒）
_last_call: dict[str, float] = {}


def _uid(event: MessageEvent) -> str:
    return event.get_session_id()


def _cooldown_left(uid: str) -> int:
    try:
        seconds = int(get("weiss.cooldown_seconds", 5))
    except (TypeError, ValueError):
        seconds = 5
    if seconds <= 0:
        return 0
    now = time.monotonic()
    last = _last_call.get(uid, 0.0)
    if now - last < seconds:
        return int(seconds - (now - last)) + 1
    _last_call[uid] = now
    return 0


def _fallback_text(card: Card) -> str:
    lines = [f"{card.name}", f"卡号：{card.no}  [{card.rarity}]"]
    meta = [f"类型 {card.kind}"] if card.kind else []
    if not card.is_climax:
        meta.append(f"等级 {card.level or '—'} 费用 {card.cost or '—'} 力量 {card.power or '—'}")
    meta.append(f"魂 {card.soul or '—'}")
    lines.append("  ".join(meta))
    if card.text:
        lines.append("")
        lines.append(card.text[:600] + ("…" if len(card.text) > 600 else ""))
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "WS 黑白双翼 卡查\n"
        "/ws WS02-E002     按卡号查（推荐）\n"
        "/ws Yukino        按卡名搜（英文）\n"
        "/ws 帮助          再看一遍这个\n"
        "\n"
        "不写系列前缀也行：/ws WS02-E002\n"
        "搜出多张会先列候选，再用完整卡号查。\n"
        "\n"
        "卡片内容：官方卡图、颜色、类型、稀有度、\n"
        "等级/费用/力量、魂、触发图标、效果全文。\n"
        "\n"
        "数据来自官方英文卡表 en.ws-tcg.com，\n"
        "所以卡名和效果是英文的。卡号三国通用。"
    )


# ---------------- /ws ----------------
ws_cmd = on_command(
    "ws",
    aliases={"wsc", "黑白双翼", "查ws", "ws卡"},
    priority=10,
)


@ws_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    if not get("weiss.enabled", True):
        await ws_cmd.finish("WS 卡查已关闭，请联系管理员")

    keyword = arg.extract_plain_text().strip()
    if not keyword or keyword in {"帮助", "help", "?", "？"}:
        await ws_cmd.finish(_help_text())

    left = _cooldown_left(_uid(event))
    if left:
        await ws_cmd.finish(f"查得太快了，{left} 秒后再试")

    try:
        cards = await search(keyword)
    except Exception:
        logger.exception(f"WS 卡查失败：{keyword}")
        await ws_cmd.finish("查询失败，官方卡表可能暂时连不上，过一会儿再试")

    if not cards:
        await ws_cmd.finish(
            f"没找到「{keyword}」。\n"
            "卡名要用英文关键词（比如 Yukino），卡号可以直接打 WS02-E002。"
        )

    try:
        limit = int(get("weiss.search_limit", 10))
    except (TypeError, ValueError):
        limit = 10

    main, _others = pick(cards, keyword)

    # 搜出多张**不同**的卡才算歧义；同一张卡的不同闪版不算
    distinct = {c.base_no for c in cards}
    exact = main.no.lower() == keyword.lower() or main.name.lower() == keyword.lower()
    if len(distinct) > 1 and not exact:
        shown = cards[:limit]
        try:
            png = await asyncio.to_thread(render_candidates, keyword, shown)
            await ws_cmd.finish(MessageSegment.file_image(png, "ws_list.png"))
        except Exception:
            logger.exception("WS 候选列表渲染失败，改发文本")
            names = "\n".join(f"{c.no}  {c.name}" for c in shown)
            await ws_cmd.finish(f"找到 {len(cards)} 张：\n{names}\n发 /ws 完整卡号 看详情")

    # 出详情：卡图和触发图标并发下载，任何一个失败都不影响出卡
    img_task = asyncio.create_task(load_image(main))
    icon_task = asyncio.create_task(load_parts(main.triggers))

    img_bytes = None
    icons: dict[str, bytes] = {}
    try:
        img_bytes, icons = await asyncio.gather(img_task, icon_task)
    except Exception:
        logger.debug("WS 素材加载异常，按缺图处理")

    try:
        png = await asyncio.to_thread(render_card, main, img_bytes, icons)
        await ws_cmd.finish(MessageSegment.file_image(png, f"ws_{main.no.replace('/', '_')}.png"))
    except Exception:
        logger.exception("WS 卡片渲染失败，改发文本")
        await ws_cmd.finish(_fallback_text(main))
