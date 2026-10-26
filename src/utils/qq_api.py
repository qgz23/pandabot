# -*- coding: utf-8 -*-
"""
QQ 官方 v2 接口的通用调用封装
================================================================
``nonebot-adapter-qq 1.7.3`` 只封装了**频道**那套（禁言、踢人、改名片），
QQ 群的接口一个都没包，所以群管理类功能只能自己拼 Request。

为什么要走 ``bot._request`` 而不是直接用 httpx / aiohttp：
它负责附加 access_token，并且在 401 时清缓存、重新取 token、重发一次。
自己发就得把这套重写一遍，还容易漏掉多机器人场景下的 token 隔离。

用法：
    from src.utils.qq_api import call_api
    await call_api(bot, ("v2", "groups", gid, "messages", mid), method="DELETE")
"""

from __future__ import annotations

from typing import Any

from nonebot.adapters.qq import Bot
from nonebot.drivers import Request


async def call_api(
    bot: Bot,
    parts: tuple[str, ...],
    *,
    method: str = "POST",
    body: dict | None = None,
) -> Any:
    """拼出 ``{api_base}/{parts...}`` 并发请求。

    失败时抛 ``nonebot.adapters.qq.exception.ActionFailed``，
    带 ``.code`` / ``.message`` / ``.status_code``，调用方按错误码翻译成人话。
    """
    url = bot.adapter.get_api_base().joinpath(*parts)
    return await bot._request(Request(method, url, json=body))


async def recall_group_message(bot: Bot, group_openid: str, message_id: str) -> Any:
    """撤回群消息。机器人必须是群管理员，且只限消息发出后 2 分钟内。"""
    return await call_api(
        bot,
        ("v2", "groups", group_openid, "messages", message_id),
        method="DELETE",
    )


__all__ = ["call_api", "recall_group_message"]
