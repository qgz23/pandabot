# -*- coding: utf-8 -*-
"""
群管理：禁言 / 解禁 / 撤回
================================================================
三条命令都靠"引用某人的消息"来指定对象。这不是偷懒，是官方 QQ 机器人
唯一可行的方式：它拿不到 QQ 号，@ 段的 user_id 也未必是 openid，
只有被引用消息的 ``author.member_openid`` 是稳定可用的。

前提（差一样都会失败，而且是接口报错、不是代码报错）：
  1. 机器人必须是群管理员
     手机 QQ → 群设置 → 群管理 → 设置管理员 → 勾上熊猫娘
  2. 禁言只能针对普通成员，群主 / 管理员 / 机器人 动不了
  3. 撤回只限消息发出后 2 分钟内

权限：默认只有群主和管理员能用（事件里的 ``author.member_role``）。
"""

from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.qq import Bot, Message, MessageEvent
from nonebot.adapters.qq.event import GroupMessageCreateEvent
from nonebot.adapters.qq.exception import ActionFailed
from nonebot.log import logger
from nonebot.params import CommandArg

from src.plugins.settings.config import get
from src.utils.qq_api import call_api

from .api import (
    DEFAULT_MUTE_MINUTES,
    explain_mute,
    explain_recall,
    humanize_minutes,
    mute_payload,
    parse_duration,
    role_label,
)

DEFAULT_ROLES = ["owner", "admin"]

# 引用/at 都拿不到目标时的统一提示。
_NO_TARGET = (
    "要指定是谁，引用他的消息再发命令。\n"
    "例：引用别人一句话，发 /禁言 10\n"
    "官方机器人拿不到 QQ 号，只能靠引用消息认人。"
)

# 底层请求统一走 src/utils/qq_api，和敏感词过滤共用一份实现。
_api = call_api


# ====================================================================
# 权限与目标识别
# ====================================================================
def _check(event: MessageEvent) -> str | None:
    """返回 None = 放行；返回字符串 = 要回给用户的拒绝理由。"""
    if not get("admin.enabled", True):
        return "群管理功能当前已关闭"

    if not isinstance(event, GroupMessageCreateEvent):
        return "这个命令只能在群里用"

    roles = get("admin.allow_roles", DEFAULT_ROLES)
    if not isinstance(roles, (list, tuple)):
        roles = DEFAULT_ROLES
    allowed = {str(r) for r in roles}

    role = str(getattr(getattr(event, "author", None), "member_role", "") or "member")
    if role not in allowed:
        return f"只有群主和管理员能用这个命令（你现在是{role_label(role)}）"
    return None


def _target(event: MessageEvent) -> tuple[str, str]:
    """找出这条命令要作用在谁身上，返回 (member_openid, 显示名)。

    两条来源，引用优先：
      1. 被引用消息的作者 —— QQ 客户端引用时会自动补一个 at，
         且 msg_elements 里带 author.member_openid，最可靠
      2. 消息里第一个不是机器人的 @

    都拿不到就返回 ("", "")，由调用方提示用法。
    """
    reply = getattr(event, "reply", None)
    author = getattr(reply, "author", None) if reply is not None else None
    if author is not None and not getattr(author, "bot", False):
        openid = str(getattr(author, "member_openid", "") or "")
        if openid:
            return openid, str(getattr(author, "username", "") or "")

    for mention in getattr(event, "mentions", None) or []:
        openid = str(getattr(mention, "member_openid", "") or "")
        if openid and not getattr(mention, "is_you", False):
            return openid, str(getattr(mention, "username", "") or "")
    return "", ""


def _default_minutes() -> int:
    """默认禁言时长，读配置；配置写坏了就退回 10，不让命令直接崩。"""
    try:
        return max(1, int(get("admin.default_mute_minutes", DEFAULT_MUTE_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_MUTE_MINUTES


# ====================================================================
# /禁言
# ====================================================================
mute_cmd = on_command("禁言", aliases={"口球", "mute"}, priority=10)


@mute_cmd.handle()
async def _handle_mute(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    deny = _check(event)
    if deny:
        await mute_cmd.finish(deny)

    openid, name = _target(event)
    if not openid:
        await mute_cmd.finish(_NO_TARGET)

    minutes, warn = parse_duration(arg.extract_plain_text(), default=_default_minutes())
    if minutes is None:
        await mute_cmd.finish(warn)

    group_openid = str(getattr(event, "group_openid", ""))
    try:
        await _api(
            bot,
            ("v2", "groups", group_openid, "restrict_chat_setting"),
            body=mute_payload(openid, minutes, op="add"),
        )
    except ActionFailed as e:
        logger.warning(
            f"禁言失败 code={e.code} msg={e.message} group={group_openid} target={openid}"
        )
        await mute_cmd.finish(explain_mute(e.code, e.message))
    except Exception:
        logger.exception("禁言接口异常")
        await mute_cmd.finish("禁言没成功，接口请求出错了，日志里有细节")

    who = f"{name} " if name else ""
    note = f"\n{warn}" if warn else ""
    await mute_cmd.finish(f"已禁言 {who}{humanize_minutes(minutes)}{note}")


# ====================================================================
# /解禁
# ====================================================================
unmute_cmd = on_command("解禁", aliases={"解除禁言", "取消禁言", "unmute"}, priority=10)


@unmute_cmd.handle()
async def _handle_unmute(bot: Bot, event: MessageEvent):
    deny = _check(event)
    if deny:
        await unmute_cmd.finish(deny)

    openid, name = _target(event)
    if not openid:
        await unmute_cmd.finish(_NO_TARGET)

    group_openid = str(getattr(event, "group_openid", ""))
    try:
        await _api(
            bot,
            ("v2", "groups", group_openid, "restrict_chat_setting"),
            body=mute_payload(openid, 0, op="del"),
        )
    except ActionFailed as e:
        logger.warning(
            f"解禁失败 code={e.code} msg={e.message} group={group_openid} target={openid}"
        )
        await unmute_cmd.finish(explain_mute(e.code, e.message))
    except Exception:
        logger.exception("解禁接口异常")
        await unmute_cmd.finish("解禁没成功，接口请求出错了，日志里有细节")

    who = name or "对方"
    await unmute_cmd.finish(f"已给 {who} 解禁")


# ====================================================================
# /撤回
# ====================================================================
recall_cmd = on_command("撤回", aliases={"撤销", "recall"}, priority=10)


@recall_cmd.handle()
async def _handle_recall(bot: Bot, event: MessageEvent):
    deny = _check(event)
    if deny:
        await recall_cmd.finish(deny)

    reply = getattr(event, "reply", None)
    message_id = str(getattr(reply, "msg_idx", "") or "") if reply is not None else ""
    if not message_id:
        await recall_cmd.finish(
            "要撤回哪条，就引用着那条消息发 /撤回。\n"
            "注意：只能撤 2 分钟内发出的，而且群主/管理员的消息撤不了。"
        )

    group_openid = str(getattr(event, "group_openid", ""))
    try:
        await _api(
            bot,
            ("v2", "groups", group_openid, "messages", message_id),
            method="DELETE",
        )
    except ActionFailed as e:
        logger.warning(
            f"撤回失败 code={e.code} msg={e.message} group={group_openid} mid={message_id}"
        )
        await recall_cmd.finish(explain_recall(e.code, e.message))
    except Exception:
        logger.exception("撤回接口异常")
        await recall_cmd.finish("撤回没成功，接口请求出错了，日志里有细节")

    await recall_cmd.finish("撤回好了")
