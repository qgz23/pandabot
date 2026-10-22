# -*- coding: utf-8 -*-
"""
配置热加载插件
================================================================
启动时拉起一个后台协程，每 3 秒检查 settings.json 的修改时间，
有变化就重新读入内存。全程不重启进程，QQ 长连接不会中断。

手动重载：
    /重载配置   （别名 /reload）

其他插件取配置：
    from src.plugins.settings.config import DATA, get
    gain = get("checkin_gain", 10)
"""

import asyncio

from nonebot import get_driver, on_command
from nonebot.adapters.qq import MessageEvent

from .config import CONFIG_PATH, DATA, load_now, watch_loop

driver = get_driver()

_watch_task: asyncio.Task | None = None


@driver.on_startup
async def _start_watch():
    global _watch_task
    _watch_task = asyncio.create_task(watch_loop())


@driver.on_shutdown
async def _stop_watch():
    if _watch_task and not _watch_task.done():
        _watch_task.cancel()


reload_cmd = on_command("重载配置", aliases={"reload"}, priority=5)


@reload_cmd.handle()
async def _(event: MessageEvent):
    if load_now():
        await reload_cmd.finish(f"配置已重载：{CONFIG_PATH.name}（{len(DATA)} 个顶层项）")
    await reload_cmd.finish("配置重载失败，请检查日志或 JSON 格式")
