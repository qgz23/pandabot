# -*- coding: utf-8 -*-
"""
小游戏：猜数字 / 猜拳 / 骰子
================================================================
    /猜数字        1~100，七次机会，之后直接发数字
    /猜拳 石头     石头 / 剪刀 / 布
    /骰子 3d20     掷骰，不写参数就是 1d6

猜数字需要一个「等待用户后续消息」的状态，这里用内存字典存，
key 是 session_id（群 + 群 + 人），所以**同一个群里的不同人互不干扰**。

为什么用内存而不是数据库：
    游戏状态是秒级的、丢了也无所谓的。写数据库只会带来连接开销，
    还要处理跨天清理。重启 bot 丢一局游戏，没人会在意。

为什么加过期时间：
    用户发完 /猜数字 就再也没猜，状态会永远挂在字典里；
    QQ 群人数多起来后这是真实的内存泄漏。5 分钟不猜就算放弃。

为什么监听器 block=False 且只认纯数字：
    群里有人在猜数字时，不能让别人的正常聊天被 bot 抢走。
    只处理「纯数字 + 当前会话确实有活跃对局」这两条同时成立的消息，
    其余一律放行给别的插件。
"""

from __future__ import annotations

import random
import re
import time

from nonebot import on_command, on_message
from nonebot.adapters.qq import Message, MessageEvent
from nonebot.params import CommandArg

# ============ 猜数字 ============

GAMES: dict[str, dict] = {}
TIMEOUT_SECONDS = 300
MAX_TRIES = 7
LOW, HIGH = 1, 100


def _gc() -> None:
    """惰性清理过期对局。不用后台任务，省一个定时器。"""
    now = time.monotonic()
    for key in [k for k, v in GAMES.items() if now - v["ts"] > TIMEOUT_SECONDS]:
        GAMES.pop(key, None)


def _session_key(event: MessageEvent) -> str:
    return event.get_session_id()


guess_start = on_command("猜数字", aliases={"猜数"}, priority=10)


@guess_start.handle()
async def _(event: MessageEvent):
    _gc()
    key = _session_key(event)

    existed = key in GAMES
    GAMES[key] = {
        "target": random.randint(LOW, HIGH),
        "left": MAX_TRIES,
        "total": MAX_TRIES,
        "ts": time.monotonic(),
    }

    head = "上一局作废，重新来一局。" if existed else ""
    await guess_start.finish(
        f"{head}我想好了一个 {LOW}~{HIGH} 之间的数字，你有 {MAX_TRIES} 次机会。\n"
        "直接发数字来猜就行（不用加斜杠），5 分钟不猜就作废。"
    )


# priority=8：比 other 命令（通常 10）先跑，才能拿到用户发的数字
guess_try = on_message(priority=8, block=False)


@guess_try.handle()
async def _(event: MessageEvent):
    _gc()
    key = _session_key(event)
    state = GAMES.get(key)
    if not state:
        return  # 没有进行中的对局，放行

    text = event.get_plaintext().strip()
    if not text.isdigit():
        return  # 不是纯数字（聊天、别的命令），放行

    number = int(text)
    if not LOW <= number <= HIGH:
        await guess_try.finish(f"猜 {LOW}~{HIGH} 之间的数哦，还剩 {state['left']} 次")

    state["left"] -= 1
    used = state["total"] - state["left"]
    target = state["target"]

    if number == target:
        GAMES.pop(key, None)
        praise = "一次命中，运气有点可怕。" if used == 1 else f"用了 {used} 次。"
        await guess_try.finish(f"猜中了，就是 {number}！{praise}")

    if state["left"] <= 0:
        GAMES.pop(key, None)
        await guess_try.finish(
            f"次数用完了，答案是 {target}。\n再来一局就发 /猜数字"
        )

    hint = "大" if number > target else "小"
    state["ts"] = time.monotonic()  # 每次猜都续期，别让认真玩的人被超时打断
    await guess_try.finish(f"{number} 太{hint}了，还剩 {state['left']} 次")


# ============ 猜拳 ============

# 0 石头 / 1 布 / 2 剪刀。这样 (自己 - 对方) % 3 == 1 就是自己赢，
# 比写一堆 if 分支清楚得多。
HANDS = {"石头": 0, "剪刀": 2, "布": 1}
HAND_NAMES = ("石头", "布", "剪刀")

rps = on_command("猜拳", priority=10)


@rps.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    choice = arg.extract_plain_text().strip()
    if choice not in HANDS:
        await rps.finish("用法：/猜拳 石头（也可以填剪刀、布）")

    mine = HANDS[choice]
    bot = random.choice((0, 1, 2))
    diff = (mine - bot) % 3

    if diff == 0:
        result = "平局，握个手。"
    elif diff == 1:
        result = "你赢了，今天手气可以。"
    else:
        result = "我赢了，但我绝对不会告诉别人。"

    await rps.finish(f"你出{HAND_NAMES[mine]}，我出{HAND_NAMES[bot]}。\n{result}")


# ============ 骰子 ============

DICE_PATTERN = re.compile(r"^(\d{0,2})[dD](\d{1,3})$")


def _parse_dice(arg: str) -> tuple[int, int]:
    """解析 /骰子 的参数，返回 (个数, 面数)。支持 3d20 和裸数字 3。"""
    text = arg.strip().replace(" ", "")
    if not text:
        return 1, 6

    m = DICE_PATTERN.match(text)
    if m:
        count = int(m.group(1) or 1)
        faces = int(m.group(2))
    elif text.isdigit():
        count, faces = int(text), 6
    else:
        raise ValueError("格式不对")

    if not 1 <= count <= 10:
        raise ValueError("个数要 1~10")
    if not 2 <= faces <= 100:
        raise ValueError("面数要 2~100")
    return count, faces


dice = on_command("骰子", aliases={"roll", "掷骰"}, priority=10)


@dice.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    try:
        count, faces = _parse_dice(arg.extract_plain_text())
    except ValueError as e:
        msg = str(e)
        if msg == "格式不对":
            await dice.finish("用法：/骰子、/骰子 3、/骰子 3d20")
        await dice.finish(f"{msg}。用法：/骰子 3d20")

    rolls = [random.randint(1, faces) for _ in range(count)]
    total = sum(rolls)

    if count == 1:
        await dice.finish(f"掷出 {rolls[0]}（1~{faces}）")

    detail = " + ".join(str(r) for r in rolls)
    await dice.finish(f"{count}d{faces}：{detail} = {total}（最大 {count * faces}）")
