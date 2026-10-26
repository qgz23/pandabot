# -*- coding: utf-8 -*-
"""
群管理接口的纯逻辑层
================================================================
只放"能脱离 NoneBot 单独测试"的东西：时长解析、请求体构造、错误码翻译。
真正发 HTTP 请求的部分在 __init__.py 里，因为那需要 bot 实例，
而 import 本模块时 NoneBot 可能还没 init —— 那样会直接抛
``ValueError: NoneBot has not been initialized``。

官方接口（2026-10 逐条核对过文档）：

  禁言  POST   /v2/groups/{group_openid}/restrict_chat_setting      60 QPM
        - 机器人必须是**群管理员**
        - 只能操作普通成员，群主 / 管理员 / 机器人 动不了
        - 单次最多 10 人，最长 30 天
        - body: {"members":[{"op":"add|update|del","member_openid":..,"mute_expire_at":..}]}

  撤回  DELETE /v2/groups/{group_openid}/messages/{message_id}      10 QPS
        - 机器人是管理员：能撤回自己的消息 + 普通成员的消息
        - 机器人是普通成员：只能撤回自己发的
        - 一律只限消息发出后 **2 分钟**内
        - 40064004 = 超出时限，40062003 = 没权限
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# 北京时间。写死 +08:00，不依赖服务器本地时区：
# 禁言到期时间要和 QQ 客户端上显示的对得上，服务器跑在 UTC 也不能偏 8 小时。
CST = timezone(timedelta(hours=8))

DEFAULT_MUTE_MINUTES = 10
MIN_MUTE_MINUTES = 1
MAX_MUTE_MINUTES = 30 * 24 * 60  # 官方上限 30 天 → 43200 分钟

# 单位换算表。键统一小写；中文原样放，避免用户被迫记英文。
UNIT_MINUTES = {
    "": 1,
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "分": 1, "分钟": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "时": 60, "小时": 60,
    "d": 1440, "day": 1440, "days": 1440,
    "天": 1440, "日": 1440,
}

# 数字 + 可选单位。单位只吃字母和中文，遇到空格、标点就停。
# 「1小时30分」会被切成 [("1","小时"), ("30","分")] 两段。
# 负号也要吃进来：否则 /禁言 -5 会被解析成「5」，把错输变成一次真实禁言。
_PART = re.compile(r"(-?\d+(?:\.\d+)?)\s*([A-Za-z\u4e00-\u9fa5]*)")

# --------------------------------------------------------------------
# 中文数字：「十分钟」「半小时」「两小时」「一天」群里肯定有人这么打。
# 先把它们换成阿拉伯数字，再走同一套解析，省得写两套逻辑。
# --------------------------------------------------------------------
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_RUN = re.compile(r"[零一二两三四五六七八九十百千半]+")


def _cn_to_number(text: str) -> float | None:
    """「十」→10、「二十五」→25、「半」→0.5、「三百」→300。

    只覆盖 0~9999，禁言时长最多 30 天，够用了。
    解析不了返回 None，调用方保留原文。
    """
    if text == "半":
        return 0.5

    total = 0
    current = 0
    for ch in text:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            # 「十五」里的「十」前面没有数字，按 1 算
            total += (current or 1) * _CN_UNITS[ch]
            current = 0
        elif ch == "半":
            total += 0.5
        else:
            return None
    return total + current


def _normalize_cn(raw: str) -> str:
    def repl(match: re.Match) -> str:
        value = _cn_to_number(match.group(0))
        return match.group(0) if value is None else str(value)

    return _CN_RUN.sub(repl, raw)

# 角色 → 中文，用于提示文案
ROLE_LABELS = {"owner": "群主", "admin": "管理员", "member": "普通成员"}


def parse_duration(
    text: str, default: int = DEFAULT_MUTE_MINUTES
) -> tuple[int | None, str]:
    """把「10」「30m」「2h」「1d」「1小时30分」解析成分钟数。

    返回 ``(分钟, 附加提示)``：

    - 正常：``(30, "")``
    - 空输入：``(default, "")`` —— 不带参数时用默认时长，不算错
    - 超上限：``(43200, "官方上限 30 天，已按 30 天处理")`` 边夹取边提示
    - 看不懂：``(None, "错误说明")``，调用方直接回这句话就行

    支持 ``10`` / ``30m`` / ``2h`` / ``1d`` / ``1.5h`` / ``1小时30分`` /
    ``十分钟`` / ``半小时`` / ``两小时`` / ``一天``。
    """
    original = (text or "").strip()
    if not original:
        return default, ""

    # 「两个小时」「三个天」里的量词「个」对换算没意义，先去掉
    raw = _normalize_cn(original).replace("个", "")
    parts = _PART.findall(raw)
    if not parts:
        return None, f"没看懂时长「{original}」，可以写 10、30m、2h、1d、1小时30分"

    total = 0.0
    for num, unit in parts:
        key = unit.strip().lower()
        if key not in UNIT_MINUTES:
            return None, f"没看懂时长「{original}」，可以写 10、30m、2h、1d、1小时30分"
        value = float(num)
        if value < 0:
            return None, "时长不能是负数"
        total += value * UNIT_MINUTES[key]

    minutes = int(round(total))
    if minutes < MIN_MUTE_MINUTES:
        return MIN_MUTE_MINUTES, f"最短只能 {MIN_MUTE_MINUTES} 分钟，已按最小值处理"
    if minutes > MAX_MUTE_MINUTES:
        return MAX_MUTE_MINUTES, "官方上限是 30 天，已按 30 天处理"
    return minutes, ""


def humanize_minutes(minutes: int) -> str:
    """43200 → '30 天'；90 → '1 小时 30 分钟'；10 → '10 分钟'。"""
    if minutes < 60:
        return f"{minutes} 分钟"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    bits: list[str] = []
    if days:
        bits.append(f"{days} 天")
    if hours:
        bits.append(f"{hours} 小时")
    if mins:
        bits.append(f"{mins} 分钟")
    return " ".join(bits) or f"{minutes} 分钟"


def mute_expire_at(minutes: int, now: datetime | None = None) -> str:
    """算出 RFC3339 格式的到期时间，例如 2026-10-26T20:30:00+08:00。

    传 ``now`` 是为了能单测；正常调用不传，取当前北京时间。
    """
    base = now.astimezone(CST) if now else datetime.now(CST)
    return (base + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def mute_payload(
    member_openid: str,
    minutes: int,
    *,
    op: str = "add",
    now: datetime | None = None,
) -> dict:
    """构造禁言请求体。

    ``op`` 只有三个合法值：``add`` 新增禁言、``update`` 改到期时间、
    ``del`` 解除禁言（此时不传 mute_expire_at，或者传空串表示立即解除）。
    """
    item: dict = {"op": op, "member_openid": member_openid}
    if op != "del":
        item["mute_expire_at"] = mute_expire_at(minutes, now)
    return {"members": [item]}


_MUTE_ERRORS = {
    40062003: (
        "接口拒绝了：要么机器人还不是群管理员，要么对方是群主 / 管理员 / 机器人。\n"
        "禁言权限要在 手机 QQ → 群设置 → 群管理 → 设置管理员 里把熊猫娘加上"
    ),
    40061001: "请求参数不合法，可能拿到的 openid 不对",
}

_RECALL_ERRORS = {
    40064004: "这条消息发出超过 2 分钟了，官方不允许再撤回",
    40062003: (
        "撤不回来：机器人要是群管理员才能撤别人的消息，"
        "而且群主 / 管理员发的消息撤不了"
    ),
    40061001: "消息 ID 无效，可能这条消息不是从群消息事件里收到的",
}


def explain_mute(code: int | None, message: str | None = None) -> str:
    """把禁言接口的错误码翻译成群里能看懂的话。"""
    if code in _MUTE_ERRORS:
        return _MUTE_ERRORS[code]
    raw = f"（{message}）" if message else ""
    return f"禁言失败，错误码 {code}{raw}"


def explain_recall(code: int | None, message: str | None = None) -> str:
    """把撤回接口的错误码翻译成群里能看懂的话。"""
    if code in _RECALL_ERRORS:
        return _RECALL_ERRORS[code]
    raw = f"（{message}）" if message else ""
    return f"撤回失败，错误码 {code}{raw}"


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role or "未知")


__all__ = [
    "CST",
    "DEFAULT_MUTE_MINUTES",
    "MAX_MUTE_MINUTES",
    "MIN_MUTE_MINUTES",
    "UNIT_MINUTES",
    "ROLE_LABELS",
    "parse_duration",
    "humanize_minutes",
    "mute_expire_at",
    "mute_payload",
    "explain_mute",
    "explain_recall",
    "role_label",
]
