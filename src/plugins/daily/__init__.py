# -*- coding: utf-8 -*-
"""
每日娱乐玩法合集
================================================================
    /今日运势     今天适不适合干点什么
    /今日猪猪     抽本群一只猪
    /今日超能力   抽一个只能维持一天的超能力
    /今日doro     抽一个 Doro 结局
    /哈基米       今日哈基米指数

设计要点：
    - 五个玩法共用 daily/service.py 的抽签引擎和 render.py 的卡片模板，
      新增玩法只要加数据池 + 注册命令，不用重写渲染。
    - **确定性**：同一天同一人重复发，结果永远一样（seed 里带日期），
      第二天自动换。不用建表记录状态。
    - 图片渲染/发送失败都会自动降级成文本，命令不会哑掉。

配置在 settings.json 的 "daily" 段，改完约 3 秒热生效。
"""

from datetime import date

from nonebot import on_command
from nonebot.adapters.qq import MessageEvent, MessageSegment
from nonebot.log import logger

from src.plugins.settings.config import get
from src.plugins.userstore.store import store

from .render import render_daily
from .service import (
    DailyResult,
    draw_doro,
    draw_fortune,
    draw_hakimi,
    draw_pig,
    draw_power,
    to_text,
)


def _group_openid(event: MessageEvent) -> str:
    return str(getattr(event, "group_openid", "") or "")


def _nickname(event: MessageEvent) -> str:
    # author 只存在于群聊/C2C 等子类事件上，基类没有；两层都要兜住
    author = getattr(event, "author", None)
    return str(getattr(author, "username", "") or "")


async def _deliver(cmd, result: DailyResult) -> None:
    """渲染并发送。图片链路任一环节失败都退回文本，绝不让命令无响应。"""
    try:
        png = render_daily(result)
    except Exception:
        logger.exception(f"{result.kind} 卡片渲染失败，回退文本")
        await cmd.finish(to_text(result))

    try:
        await cmd.send(MessageSegment.file_image(png, f"{result.kind}.png"))
    except Exception:
        logger.exception(f"{result.kind} 卡片发送失败，回退文本")
        await cmd.finish(to_text(result))


async def _play(cmd, event: MessageEvent, kind: str, builder) -> None:
    if not get("daily.enabled", True):
        await cmd.finish("娱乐功能已关闭，请联系管理员")

    module_on = get(f"daily.{kind}_enabled", True)
    if not module_on:
        await cmd.finish("这个玩法管理员暂时关掉了")

    date_str = date.today().isoformat()
    user_id = event.get_user_id()
    group_openid = _group_openid(event)

    # 顺手把自己记进用户表：命令消息会被 on_command 的 block 拦下，
    # 走不到 userstore 的全局监听，不补这一下「今日猪猪」的候选池会越来越小。
    try:
        await store.touch(event.get_session_id(), user_id, _nickname(event))
    except Exception:
        logger.debug("touch 失败，不影响抽签", exc_info=True)

    result = await builder(
        date_str=date_str,
        user_id=user_id,
        group_openid=group_openid,
        event=event,
    )
    await _deliver(cmd, result)


# ====================================================================
# 注册五个命令
# ====================================================================

fortune_cmd = on_command("今日运势", aliases={"运势", "今日签"}, priority=10)


@fortune_cmd.handle()
async def _(event: MessageEvent):
    async def build(*, date_str, user_id, group_openid, event):
        return draw_fortune(date_str=date_str, user_id=user_id, group_id=group_openid)

    await _play(fortune_cmd, event, "fortune", build)


pig_cmd = on_command("今日猪猪", aliases={"猪猪", "今日猪"}, priority=10)


@pig_cmd.handle()
async def _(event: MessageEvent):
    async def build(*, date_str, user_id, group_openid, event):
        # 候选池 = 本群说过话的人（userstore 自动建档）。
        # QQ 官方机器人没有拿群成员列表的权限，这是唯一可行的来源。
        members = await store.list_group_members(group_openid) if group_openid else []
        return draw_pig(
            date_str=date_str,
            user_id=user_id,
            group_id=group_openid,
            members=members,
        )

    await _play(pig_cmd, event, "pig", build)


power_cmd = on_command("今日超能力", aliases={"超能力", "我的超能力"}, priority=10)


@power_cmd.handle()
async def _(event: MessageEvent):
    async def build(*, date_str, user_id, group_openid, event):
        return draw_power(date_str=date_str, user_id=user_id, group_id=group_openid)

    await _play(power_cmd, event, "power", build)


doro_cmd = on_command("今日doro", aliases={"doro", "Doro", "DORO", "今日Doro"}, priority=10)


@doro_cmd.handle()
async def _(event: MessageEvent):
    async def build(*, date_str, user_id, group_openid, event):
        return draw_doro(date_str=date_str, user_id=user_id, group_id=group_openid)

    await _play(doro_cmd, event, "doro", build)


hakimi_cmd = on_command("哈基米", aliases={"hakimi", "今日哈基米"}, priority=10)


@hakimi_cmd.handle()
async def _(event: MessageEvent):
    async def build(*, date_str, user_id, group_openid, event):
        return draw_hakimi(date_str=date_str, user_id=user_id, group_id=group_openid)

    await _play(hakimi_cmd, event, "hakimi", build)
