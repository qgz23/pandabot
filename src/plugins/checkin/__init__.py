# -*- coding: utf-8 -*-
"""
每日签到与积分查询插件
================================================================
/签到（打卡 / checkin）：每人每天限签一次，签到得固定积分（默认 10）
/积分（我的积分 / score）：查询当前总积分

积分与签到记录都存在 MySQL（复用 userstore 的 store 连接池）。
"""

from datetime import date

from nonebot import on_command
from nonebot.adapters.qq import MessageEvent

from src.plugins.userstore.store import store

# 每次签到获得的积分（可按需调整）
DAILY_GAIN = 10


def _resolve(event: MessageEvent) -> tuple[str, str, str]:
    uid = event.get_session_id()
    openid = event.get_user_id()
    nickname = getattr(event.author, "username", "") or "朋友"
    return uid, openid, nickname


# ============ 签到 ============
checkin_cmd = on_command("签到", aliases={"打卡", "checkin"}, priority=10)


@checkin_cmd.handle()
async def _(event: MessageEvent):
    uid, openid, nickname = _resolve(event)
    await store.touch(uid, openid, nickname)  # 确保用户已建档
    today = date.today().isoformat()

    ok, total = await store.checkin(uid, today, DAILY_GAIN)
    if ok:
        await checkin_cmd.send(
            f"{nickname}，签到成功！本次 +{DAILY_GAIN} 积分，当前总积分：{total}"
        )
    else:
        await checkin_cmd.send(
            f"{nickname}，今天已经签到过啦～当前总积分：{total}"
        )


# ============ 查询积分 ============
score_cmd = on_command("积分", aliases={"我的积分", "score", "积分查询"}, priority=10)


@score_cmd.handle()
async def _(event: MessageEvent):
    uid, _, _ = _resolve(event)
    total = await store.get_score(uid)
    await score_cmd.send(f"当前总积分：{total}")
