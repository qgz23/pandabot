# -*- coding: utf-8 -*-
"""
群友老婆 / 群友老公
================================================================
/群友老婆   从本群发过言的群友里抽一位当今天的老婆（纯文字）
/群友老公   同一套玩法，换一个签池

和 /今日老婆 的区别：
    /今日老婆  抽的是群友**上传的角色图片**（waifu 插件），出图
    /群友老婆  抽的是本群**活人**（这个插件），出文字
两个功能共用同一批人不同的池子，可以同时存在，互不影响。

候选池 = userstore 里本群有发言记录的人，所以：
    - 潜水的人抽不到（没记录）
    - 抽到的人 = 最近说过话的人，池子天然是"活跃群友"
    - 自己永远不会抽到自己

确定性：同一天同一个人抽到的永远是同一位，第二天零点自动换。
不落库、不占状态，重启也不会变。
"""

from dataclasses import replace
from datetime import date

from nonebot import on_command
from nonebot.adapters.qq import MessageEvent

from src.plugins.settings.config import get
from src.plugins.userstore.store import store

from .service import draw, to_text


def _group(event: MessageEvent) -> str:
    return str(getattr(event, "group_openid", "") or "")


def _nickname(event: MessageEvent) -> str:
    author = getattr(event, "author", None)
    return str(getattr(author, "username", "") or "") or "群友"


async def _play(cmd, event: MessageEvent, salt: str) -> None:
    if not get("couple.enabled", True):
        await cmd.finish("群友老婆功能已关闭，请联系管理员")

    group = _group(event)
    if not group:
        await cmd.finish("这个玩法要在群里用哦～")

    uid = event.get_session_id()

    # 先给自己建档，否则"首次发言就抽签"的人不在成员表里
    await store.touch(uid, event.get_user_id(), _nickname(event))

    limit = int(get("couple.pool_limit", 300) or 300)
    members = await store.list_group_members(group, limit)

    result = draw(
        date_str=date.today().isoformat(),
        group_openid=group,
        user_uid=uid,
        members=members,
        salt=salt,
    )

    if result is None:
        await cmd.finish(
            "本群里除了你还没别人发过言，抽不出来。\n"
            "等群里热闹一点再来，或者先 /今日老婆 抽个角色～"
        )

    # 抽中的人如果录过 /信息，就把 CN 带上——社团里 CN 比 QQ 昵称好认。
    # 只查这一个人，开销可以忽略
    data = await store.all(result.uid)
    cn = str(data.get("cn") or "").strip()
    if cn:
        result = replace(result, cn=cn)

    await cmd.finish(to_text(result))


wife_cmd = on_command("群友老婆", aliases={"群友对象", "抽群友"}, priority=10)


@wife_cmd.handle()
async def _(event: MessageEvent):
    await _play(wife_cmd, event, "wife")


husband_cmd = on_command("群友老公", priority=10)


@husband_cmd.handle()
async def _(event: MessageEvent):
    await _play(husband_cmd, event, "husband")
