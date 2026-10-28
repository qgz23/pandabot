# -*- coding: utf-8 -*-
"""
明日方舟干员图鉴
================================================================
    /干员 阿米娅      查干员，出图
    /干员 amiya       英文名也行（appellation 字段）
    /方舟             看数据版本和用法
    /方舟更新         强制重新拉数据（换了新干员卡池时用）

分层和舞萌那套保持一致：
    mapping.py   ← 中文映射表，无依赖，纯数据
    service.py   ← 数据下载/精简/缓存/搜索，不碰 nonebot 的消息类型
    render.py    ← 纯 Pillow 画卡
    __init__.py  ← 本文件：命令注册、冷却、发图

数据不是每次查都下载。首次启动在后台预热一遍（25 MB，3 秒左右），
之后所有查询只读本地 300 KB 的精简库。启动预热故意放在后台任务里：
网络不通时不该把 bot 启动卡住，用户第一次查再重试就行。

出图三步都可能失败（数据没就绪 / 立绘超时 / 画图报错），
任何一步炸了都退化成文字版 —— 查图鉴功能不能因为画不出图就废掉。
"""

import asyncio
import time

from nonebot import get_driver, on_command
from nonebot.adapters.qq import Message, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from src.plugins.settings.config import get

from .render import render_card, render_candidates
from .service import index, load_portrait

driver = get_driver()

# 每人上次查询时间（monotonic 秒）。出图有 CPU 开销，连着刷会拖慢整个 bot
_last_call: dict[str, float] = {}

# 后台预热任务要留引用，否则可能被 GC 回收
_warm_task: asyncio.Task | None = None

# 立绘下载的总时限。超过就不等了，发没图的版本 ——
# 一张立绘值不了 10 秒的等待，用户早就以为 bot 死了
PORTRAIT_TIMEOUT = 15.0


def _uid(event: MessageEvent) -> str:
    return event.get_session_id()


def _cooldown_left(uid: str) -> int:
    """返回还要等几秒；0 表示可以查，并顺手记下本次时间。"""
    try:
        seconds = int(get("arknights.cooldown_seconds", 5))
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


def _fallback_text(char) -> str:
    """出图失败时的文字版。信息比图片少，但至少能看。"""
    lines = [
        f"{char.name}（{char.en}）",
        f"{'★' * char.rarity}  {char.faction}",
        f"特性：{char.trait or '—'}",
    ]
    for talent in char.talents[:3]:
        lines.append(f"天赋 [{talent['name']}] {talent['desc']}")
    if char.skills:
        lines.append("技能：" + " / ".join(char.skills))
    if char.stats:
        st = char.stats
        lines.append(
            f"属性：生命 {st.get('hp')} 攻击 {st.get('atk')} 防御 {st.get('def')} "
            f"法抗 {st.get('res')} 部署 {st.get('cost')}"
        )
    return "\n".join(lines)


def _help_text() -> str:
    version = f"\n当前库：{index.count} 名干员" if index.chars else ""
    return (
        "明日方舟干员图鉴\n"
        "/干员 阿米娅   查干员（出图）\n"
        "/干员 amiya    英文名也行\n"
        "/方舟更新      拉取最新数据\n"
        "\n"
        "搜到多个会先列候选，再发全名看详情。\n"
        "属性口径：最高精英阶段的满级属性。"
        f"{version}"
    )


# ---------------- 启动预热 ----------------
@driver.on_startup
async def _warmup() -> None:
    """后台把数据拉好，别阻塞启动。"""
    global _warm_task
    if not get("arknights.enabled", True):
        return

    async def _run() -> None:
        ok = await index.ensure()
        if ok:
            logger.info(f"方舟图鉴预热完成：{index.count} 名干员")
        else:
            logger.warning("方舟图鉴预热失败（网络？），首次查询时会再试")

    _warm_task = asyncio.create_task(_run())


# ---------------- /干员 ----------------
char_cmd = on_command("干员", aliases={"图鉴", "查干员", "方舟干员"}, priority=10)


@char_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    if not get("arknights.enabled", True):
        await char_cmd.finish("图鉴功能已关闭，请联系管理员")

    keyword = arg.extract_plain_text().strip()
    if not keyword:
        await char_cmd.finish("用法：/干员 干员名\n例如：/干员 阿米娅\n完整说明：/方舟")

    left = _cooldown_left(_uid(event))
    if left:
        await char_cmd.finish(f"查得太快了，{left} 秒后再试")

    if not await index.ensure():
        await char_cmd.finish("数据还没准备好（可能是网络问题），过一会儿再试，或发 /方舟更新 重试")

    try:
        limit = int(get("arknights.search_limit", 8))
    except (TypeError, ValueError):
        limit = 8

    hits = index.search(keyword, limit=limit)
    if not hits:
        await char_cmd.finish(
            f"没找到「{keyword}」。\n"
            "试试只打一个字（比如 /干员 德），或者用英文名。"
        )

    # 多个结果时：只有第一个是「完全匹配」才直接出卡，否则先给候选表。
    # 不然搜「阿」会直接跳到某个用户没想要的干员。
    if len(hits) > 1:
        first = hits[0]
        exact = first.name == keyword or first.en.lower() == keyword.lower()
        if not exact:
            try:
                png = await asyncio.to_thread(render_candidates, keyword, hits)
                await char_cmd.finish(MessageSegment.file_image(png, "arknights_list.png"))
            except Exception:
                logger.exception("候选列表渲染失败，改发文本")
                names = "、".join(c.name for c in hits)
                await char_cmd.finish(f"找到这些干员：{names}\n发 /干员 全名 看详情")

    char = hits[0]

    portrait = None
    try:
        portrait = await asyncio.wait_for(load_portrait(char.id), timeout=PORTRAIT_TIMEOUT)
    except Exception:
        # 超时/断网都当「没有立绘」处理，卡片本身照画
        portrait = None

    try:
        png = await asyncio.to_thread(render_card, char, portrait)
        await char_cmd.finish(MessageSegment.file_image(png, f"arknights_{char.id}.png"))
    except Exception:
        logger.exception("干员卡渲染失败，改发文本")
        await char_cmd.finish(_fallback_text(char))


# ---------------- /方舟更新 ----------------
update_cmd = on_command("方舟更新", aliases={"更新图鉴"}, priority=10)


@update_cmd.handle()
async def _():
    if not get("arknights.enabled", True):
        await update_cmd.finish("图鉴功能已关闭，请联系管理员")

    # 防连点：一次强制更新要下 25 MB，被连着刷会把出口带宽吃满
    age = time.time() - index.updated_at
    if index.chars and age < 300:
        await update_cmd.finish(f"刚更新过（{int(age)} 秒前），当前 {index.count} 名干员")

    try:
        await index.refresh(force=True)
    except Exception as e:
        logger.exception("方舟数据强制更新失败")
        await update_cmd.finish(f"更新失败：{type(e).__name__}，继续使用旧数据")

    await update_cmd.finish(f"已更新，当前 {index.count} 名干员")


# ---------------- /方舟 ----------------
help_cmd = on_command("方舟", aliases={"方舟帮助", "图鉴帮助"}, priority=10)


@help_cmd.handle()
async def _():
    await help_cmd.finish(_help_text())
