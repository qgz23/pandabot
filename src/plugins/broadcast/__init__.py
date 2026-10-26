# -*- coding: utf-8 -*-
"""
定时播报插件
================================================================
让机器人在群里「时不时自己说句话」。说什么、多久说一次、轮播还是随机，
全在 **config/broadcast.json**，改完存盘最多 30 秒自动生效，不用重启。

为什么不用 nonebot-plugin-apscheduler：
    这需求本质是「每 30 秒看一眼谁到点了」，一个 asyncio 循环就够。
    引入调度库还得处理「改了 cron 怎么重新注册」「重启后错过的任务要不要补」
    这些麻烦事，反而更绕。

为什么主动发消息能成：
    QQ 官方允许机器人发「主动消息」（请求里不带 msg_id 就走这条路）。
    群聊频控：Bot 维度 60 次/分钟（未认证 30）、单群 20 次/分钟、每群每天 1000 条。
    间隔几十分钟发一条，离上限远得很。
    但用户可以在 QQ 客户端关掉「接收主动消息」，关了就发不出去 ——
    所以发送失败一律吞掉记日志，绝不能让整个循环崩掉。

群 openid 从哪来：
    机器人只有收到某个群的消息（或被拉进那个群）才知道它的 group_openid。
    所以这里挂了优先级最低的监听，见到群消息就把群记下来，
    存进 data/broadcast/state.json。没说过话的群发不了，这是平台机制决定的。
"""

import asyncio
import random
import time
from datetime import datetime

from nonebot import get_bot, get_driver, on_command, on_message
from nonebot.adapters.qq import GroupMessageCreateEvent, Message, MessageEvent
from nonebot.log import logger
from nonebot.params import CommandArg

from src.plugins.settings.config import get

from .feed import FeedBook, StateStore, may_send, today_str

driver = get_driver()

feed = FeedBook()
state = StateStore()

# 路径交给配置决定，改 settings.json 就能换地方，不用动代码
feed.set_path_provider(lambda: get("broadcast.config_file", "") or None)
state.set_path_provider(lambda: get("broadcast.state_file", "") or None)

TICK = 30.0            # 每 30 秒看一次，开销可以忽略
RETRY_LATER = 600.0    # 落在静音时段里，10 分钟后再判断
SEND_GAP = 1.0         # 多群连发时留点间隔，别一瞬间全推出去
MANUAL_COOLDOWN = 300.0  # 「/播报 立即」同一个群的冷静期

_loop_task: asyncio.Task | None = None
_last_manual: dict[str, float] = {}


# ====================================================================
# 发送
# ====================================================================
async def _send(group_openid: str, text: str) -> bool:
    """发一条主动消息。失败只记日志，返回 False。"""
    try:
        bot = get_bot()
    except ValueError:
        return False      # 还没连上
    try:
        await bot.send_to_group(group_openid=group_openid, message=text)
        return True
    except Exception as e:
        # 常见原因：用户在客户端关了主动消息、被频控、群已解散
        logger.warning(f"播报发不出去（群 {group_openid}）：{e!r}")
        return False


def _targets() -> list[str]:
    """要发往哪些群。配置里写了 groups 就只发那些，否则发所有登记过的群。"""
    if feed.group_filter:
        return list(feed.group_filter)
    return list(state.groups)


# ====================================================================
# 每轮检查
# ====================================================================
async def _tick() -> None:
    feed.ensure_fresh()

    if not get("broadcast.enabled", True) or not feed.enabled:
        return
    if not feed.tasks:
        return

    groups = _targets()
    if not groups:
        return

    now = time.time()
    hour = datetime.now().hour
    day = today_str()
    state.prune_counter(day)

    # 配置里已经被删掉的任务，顺手清掉它的进度，别留垃圾
    alive = {t.name for t in feed.tasks}
    for name in [n for n in state.tasks if n not in alive]:
        state.drop_task(name)

    for task in feed.tasks:
        st = state.task(task.name)
        nxt = st.get("next")

        # 第一次见到这个任务：不要立刻发（机器人刚起来就往群里说话很突兀），
        # 错开一个间隔的 30%~100%，多个任务也就自然散开了
        if not isinstance(nxt, (int, float)):
            st["next"] = now + random.uniform(0.3, 1.0) * task.next_gap()
            continue

        if now < nxt:
            continue

        # quiet_hours 是「禁发时段」，任务自己的 hours 是「只在这个时段发」，
        # 两者含义相反，交给 may_send 一起判断
        if not may_send(hour, feed.quiet_hours, task.hours):
            st["next"] = now + RETRY_LATER
            continue
        text, index = feed.pick(
            task, int(st.get("index", 0) or 0), str(st.get("last_text", ""))
        )
        if not text:
            continue

        # 先推进进度再发：万一发送炸了也不会变成「每 30 秒重试一次」
        st["index"] = index
        st["last_text"] = text
        st["next"] = now + task.next_gap()

        content = feed.render(text)
        sent = 0
        for gid in groups:
            if not feed.allow_group(gid):
                continue
            if state.count(day, gid) >= feed.daily_limit:
                continue
            if await _send(gid, content):
                state.bump(day, gid)
                sent += 1
                await asyncio.sleep(SEND_GAP)

        if sent:
            logger.info(f"播报「{task.name}」已发往 {sent} 个群：{content[:30]}")


async def _run_loop() -> None:
    while True:
        await asyncio.sleep(TICK)
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 单轮出错不能让整个播报停摆
            logger.exception("播报循环出错（已忽略，下一轮继续）")
        try:
            state.flush()
        except Exception:
            pass


def _repair_schedule() -> None:
    """启动时修正排期，避免「一上线就往群里说话」。

    两种情况都要重排：
      1. 这个任务从来没发过（state 里没有 next）
      2. 上次的 next 早就过期了（机器人关了两天，现在一启动就"补发"）
    重排成 now + 间隔的 30%~100%，所以任务越多越自然错开。
    """
    now = time.time()
    for task in feed.tasks:
        st = state.task(task.name)
        nxt = st.get("next")
        if not isinstance(nxt, (int, float)) or nxt <= now:
            st["next"] = now + random.uniform(0.3, 1.0) * task.next_gap()
    state.flush(force=True)


# ====================================================================
# 生命周期
# ====================================================================
@driver.on_startup
async def _start() -> None:
    global _loop_task

    if feed.write_default():
        logger.info(f"定时播报：已生成模板 {feed.path}，照着改就行")

    ok, msg = feed.load(force=True)
    if ok:
        logger.info(f"定时播报：{msg}（{feed.path}）")
    else:
        logger.warning(f"定时播报没读成功：{msg}（文件：{feed.path}）")

    state.load()
    _repair_schedule()
    _loop_task = asyncio.create_task(_run_loop())


@driver.on_shutdown
async def _stop() -> None:
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
    state.flush(force=True)


# ====================================================================
# 群登记：见到群消息就记下 group_openid，之后才能往这个群主动发
# 优先级压到最低、block=False，绝不抢任何命令的活
# ====================================================================
_group_watch = on_message(priority=100, block=False)


@_group_watch.handle()
async def _(event: MessageEvent):
    if not isinstance(event, GroupMessageCreateEvent):
        return      # 单聊没有群概念
    gid = event.group_openid
    if state.touch_group(gid):
        logger.info(f"播报：记下新群 {gid}（之后可以往这里发）")


# ====================================================================
# 命令
# ====================================================================
def _status() -> str:
    if not feed.tasks:
        return f"定时播报：一个任务都没有。\n文件：{feed.path}"

    running = feed.enabled and bool(get("broadcast.enabled", True))
    lines = [f"定时播报：{'开启中' if running else '已关闭'}（{len(_targets())} 个群）"]

    if feed.quiet_hours:
        start, end = feed.quiet_hours
        lines.append(f"静音时段：{start} 点 ~ {end} 点")
    lines.append(f"每日上限：{feed.daily_limit} 条/群")
    lines.append("")

    now = time.time()
    for task in feed.tasks[:8]:
        st = state.task(task.name)
        nxt = st.get("next")
        when = (
            datetime.fromtimestamp(nxt).strftime("%m-%d %H:%M")
            if isinstance(nxt, (int, float)) and nxt > now
            else "待定"
        )
        lines.append(f"· {task.name}｜{task.mode_label}｜每 {task.every_label}｜下次 {when}")
    if len(feed.tasks) > 8:
        lines.append(f"…… 还有 {len(feed.tasks) - 8} 个")

    lines.append("")
    lines.append("想立刻试一条：/播报 立即 [任务名]")
    lines.append(f"改文案：{feed.path.name}")
    return "\n".join(lines)


feed_cmd = on_command("播报", aliases={"定时播报", "broadcast"}, priority=10)


@feed_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    raw = arg.extract_plain_text().strip()

    if raw.startswith(("立即", "马上", "测试", "now")):
        await _manual(event, raw)
        return

    await feed_cmd.finish(_status())


async def _manual(event: MessageEvent, raw: str) -> None:
    gid = getattr(event, "group_openid", "")
    if not gid:
        await feed_cmd.finish("主动消息只能发到群里，单聊这边发不了")

    keyword = raw
    for word in ("立即", "马上", "测试", "now"):
        keyword = keyword.replace(word, "")
    keyword = keyword.strip()

    task = feed.find(keyword) if keyword else (feed.tasks[0] if feed.tasks else None)
    if task is None:
        names = "、".join(t.name for t in feed.tasks) or "（空）"
        await feed_cmd.finish(f"没找到任务「{keyword}」。现有任务：{names}")

    now = time.time()
    left = MANUAL_COOLDOWN - (now - _last_manual.get(gid, 0))
    if left > 0:
        await feed_cmd.finish(f"刚试过一次，{int(left)} 秒后再来（防止被拿去刷群）")

    st = state.task(task.name)
    text, index = feed.pick(
        task, int(st.get("index", 0) or 0), str(st.get("last_text", ""))
    )
    if not text:
        await feed_cmd.finish(f"任务「{task.name}」里没有句子")

    st["index"] = index
    st["last_text"] = text
    # 手动发过了，把自动的计时重新算，免得刚试完马上又来一条
    st["next"] = now + task.next_gap()

    _last_manual[gid] = now
    state.touch_group(gid)
    state.flush(force=True)

    await feed_cmd.finish(feed.render(text))


reload_cmd = on_command("重载播报", aliases={"重载定时播报"}, priority=10)


@reload_cmd.handle()
async def _():
    ok, msg = feed.load(force=True)
    if ok:
        await reload_cmd.finish(f"{msg}\n文件：{feed.path}")
    await reload_cmd.finish(f"没读成功，继续沿用旧配置。\n{msg}\n文件：{feed.path}")
