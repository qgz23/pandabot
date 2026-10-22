# -*- coding: utf-8 -*-
"""
舞萌查分插件（水鱼查分器）
================================================================
/绑定舞萌 <用户名>   绑定账号（会先真查询验证一遍，查不到就不落库）
/解绑舞萌            清除绑定
/b50 [用户名]        查 B50，不带参数就查自己绑定的
/查歌 <关键词>       搜曲目、看定数
/舞萌帮助            指令说明

分层：
    service.py    ← 纯业务 + HTTP，无 nonebot 依赖，可独立测试
    formatter.py  ← 纯文本渲染，无 nonebot 依赖，改样式只改这里
    __init__.py   ← 本文件：只做命令注册、参数解析、冷却、读写用户绑定

配置在 settings.json 的 "maimai" 段，改完约 3 秒热生效，不用重启。
Developer-Token 不进 Git，放 .env.prod：DF_DEV_TOKEN=你的token
（公开 B50 查询不需要 token，不配也能用）
"""

import asyncio
import time

from nonebot import get_driver, on_command
from nonebot.adapters.qq import Message, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from src.plugins.settings.config import get
from src.plugins.userstore.store import store

from .covers import covers
from .formatter import format_b50, format_bind_ok, format_help, format_search
from .render import render_b50, render_b50_grid
from .service import MaimaiError, maimai

driver = get_driver()

# 每个用户上次成功查询的时间点（monotonic 秒），用于节流
_last_call: dict[str, float] = {}

# 配置签名缓存：只有 token / 超时 / 缓存时长变了才重建连接池
_cfg_sig: tuple | None = None

# 后台预载任务要留个引用，否则可能被 GC 掉
_warm_task: asyncio.Task | None = None


# ---------------- 工具 ----------------
def _uid(event: MessageEvent) -> str:
    """存储键。沿用 userstore 的约定：session_id，含群/私聊场景。
    注意这意味着「在 A 群绑定的账号，在 B 群要重新绑」，
    是按现有数据表设计走的，要改成全群通用得动 userstore 的键规则。"""
    return event.get_session_id()


def _nickname(event: MessageEvent) -> str:
    # author 只在群聊 / C2C 子类事件上有，基类没有，两层都要兜
    author = getattr(event, "author", None)
    return str(getattr(author, "username", "") or "") or "朋友"


async def _gate(cmd) -> None:
    """功能总开关。关掉后所有指令直接回一句提示。"""
    if not get("maimai.enabled", True):
        await cmd.finish("舞萌查分功能已关闭，请联系管理员")


def _cooldown_left(uid: str) -> int:
    """返回还要等几秒；0 表示可以查，并顺手记下本次时间。"""
    try:
        seconds = int(get("maimai.cooldown_seconds", 15))
    except (TypeError, ValueError):
        seconds = 15
    if seconds <= 0:
        return 0

    now = time.monotonic()
    left = seconds - (now - _last_call.get(uid, 0.0))
    if left > 0:
        return int(left) + 1

    # 防止长期运行后字典无限膨胀
    if len(_last_call) > 2000:
        cutoff = now - seconds * 4
        for k in [k for k, v in _last_call.items() if v < cutoff]:
            _last_call.pop(k, None)

    _last_call[uid] = now
    return 0


def _sync_config() -> None:
    """把 settings.json + 环境变量里的值同步到 service。
    每次查询前调一次，这样改配置能热生效，而不用重启进程。"""
    global _cfg_sig

    token = str(getattr(driver.config, "df_dev_token", "") or "").strip()
    try:
        timeout = float(get("maimai.request_timeout", 20))
    except (TypeError, ValueError):
        timeout = 20.0
    try:
        ttl_hours = float(get("maimai.song_cache_hours", 6))
    except (TypeError, ValueError):
        ttl_hours = 6.0

    sig = (token, timeout, ttl_hours)
    if sig == _cfg_sig:
        return
    maimai.configure(token=token, timeout=timeout, song_ttl=ttl_hours * 3600)
    _cfg_sig = sig


# ---------------- 启动 / 关闭 ----------------
@driver.on_startup
async def _on_startup() -> None:
    global _warm_task
    _sync_config()
    if not get("maimai.enabled", True):
        return
    # 曲库 ~900KB，放后台预载，不拖慢 bot 启动
    _warm_task = asyncio.create_task(_preload_songs())


async def _preload_songs() -> None:
    try:
        songs = await maimai.songs()
        logger.info(f"舞萌曲库预载完成：{len(songs)} 条")
    except MaimaiError as e:
        logger.warning(f"舞萌曲库预载失败（不影响启动，首次查歌会重试）：{e}")
    except Exception as e:
        logger.warning(f"舞萌曲库预载异常：{e!r}")


@driver.on_shutdown
async def _on_shutdown() -> None:
    if _warm_task and not _warm_task.done():
        _warm_task.cancel()
    await covers.aclose()
    await maimai.aclose()


async def _render_grid(res) -> bytes:
    """渲染「成分图」（50 格封面网格）。

    封面下载有时间预算：超时就用已有封面先出图，
    没下完的任务继续在后台跑，下次查询自然就是完整图。
    """
    try:
        budget = float(get("maimai.cover_budget", 8))
    except (TypeError, ValueError):
        budget = 8.0

    ids = [c.song_id for c in res.b15] + [c.song_id for c in res.b35]
    covers_map = await covers.load_many(ids, budget=max(0.0, budget))

    missing = len(set(ids)) - len(covers_map)
    note = f"{missing} 首曲绘缓存中，稍后再查即完整" if missing > 0 else ""

    # 50 格贴图 + 描边是纯 CPU 活（约 0.4 秒），必须扔到线程里，
    # 否则会卡住整个事件循环，别的群发命令也没反应。
    return await asyncio.to_thread(render_b50_grid, res, covers_map, note=note)


# ---------------- /绑定舞萌 ----------------
bind_cmd = on_command("绑定舞萌", aliases={"绑定b50", "绑舞萌"}, priority=10)


@bind_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    await _gate(bind_cmd)

    username = arg.extract_plain_text().strip()
    if not username:
        await bind_cmd.finish(
            "用法：/绑定舞萌 你的水鱼用户名\n"
            "（用户名是你在水鱼查分器网站上的登录名，不是游戏内昵称）"
        )

    uid = _uid(event)
    left = _cooldown_left(uid)
    if left:
        await bind_cmd.finish(f"操作太频繁了，{left} 秒后再试")

    _sync_config()
    try:
        # 关键设计：先真查一遍再落库，避免绑了个不存在的名字，之后每次查都报错
        res = await maimai.fetch_b50(username)
    except MaimaiError as e:
        await bind_cmd.finish(f"绑定失败：{e}")

    await store.touch(uid, event.get_user_id(), _nickname(event))
    await store.set(uid, "maimai_username", res.username)
    await bind_cmd.finish(format_bind_ok(res))


# ---------------- /解绑舞萌 ----------------
unbind_cmd = on_command("解绑舞萌", aliases={"解绑b50"}, priority=10)


@unbind_cmd.handle()
async def _(event: MessageEvent):
    await _gate(unbind_cmd)

    uid = _uid(event)
    old = await store.get(uid, "maimai_username")
    if not old:
        await unbind_cmd.finish("你还没有绑定过舞萌账号，直接发 /绑定舞萌 用户名 就行")

    await store.set(uid, "maimai_username", "")
    await unbind_cmd.finish(f"已解绑：{old}")


# ---------------- /b50 ----------------
b50_cmd = on_command("b50", aliases={"B50", "查分"}, priority=10)


@b50_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    await _gate(b50_cmd)

    uid = _uid(event)
    username = arg.extract_plain_text().strip()

    if not username:
        username = (await store.get(uid, "maimai_username") or "").strip()
        if not username:
            await b50_cmd.finish(
                "你还没绑定账号。\n"
                "先发 /绑定舞萌 你的水鱼用户名，或者直接用 /b50 用户名"
            )

    left = _cooldown_left(uid)
    if left:
        await b50_cmd.finish(f"查得太快了，{left} 秒后再试")

    _sync_config()
    try:
        res = await maimai.fetch_b50(username)
    except MaimaiError as e:
        await b50_cmd.finish(f"查询失败：{e}")

    try:
        old_top = int(get("maimai.b50_old_top", 5))
    except (TypeError, ValueError):
        old_top = 5

    # 优先发图片。图片失败不能让命令变哑巴，所以任何一步异常都回退到文本版本。
    if get("maimai.b50_image", True):
        style = str(get("maimai.b50_style", "grid") or "grid").strip().lower()
        try:
            if style == "grid":
                png = await _render_grid(res)
            else:
                png = await asyncio.to_thread(render_b50, res, old_top=old_top)
            await b50_cmd.send(MessageSegment.file_image(png, "b50.png"))
            return
        except Exception:
            logger.exception("B50 图片渲染/发送失败，改发文本")

    await b50_cmd.finish(format_b50(res, old_top=old_top))


# ---------------- /查歌 ----------------
search_cmd = on_command("查歌", aliases={"搜歌", "找歌"}, priority=10)


@search_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    await _gate(search_cmd)

    keyword = arg.extract_plain_text().strip()
    if not keyword:
        await search_cmd.finish("用法：/查歌 曲名关键词\n例如：/查歌 雨露")

    try:
        limit = int(get("maimai.search_limit", 10))
    except (TypeError, ValueError):
        limit = 10

    _sync_config()
    try:
        hits = await maimai.search(keyword, limit=limit)
    except MaimaiError as e:
        await search_cmd.finish(f"搜索失败：{e}")

    await search_cmd.finish(format_search(hits, keyword))


# ---------------- /舞萌帮助 ----------------
help_cmd = on_command("舞萌帮助", aliases={"maimai帮助", "b50帮助", "舞萌指令"}, priority=10)


@help_cmd.handle()
async def _():
    await _gate(help_cmd)
    await help_cmd.finish(format_help())
