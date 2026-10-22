# -*- coding: utf-8 -*-
"""
用户数据存储插件：以「用户ID」为键的数据读取 / 写入（MySQL）
================================================================
存储组件见同目录 store.py（aiomysql 异步，可独立测试、可复用）。

行为（B 方案：消息即建档）：
    - 全局监听：只要收到某用户的消息，就自动建档 / 对话次数 +1（不回话、不阻断命令）
    - 我的信息 / me / 我的资料  → 读取并回显当前用户已存的数据
    - 记录 <内容> / note / 记下 → 把内容写入当前用户的 note 字段

扩展自己的字段：await store.set(uid, "字段名", 值)。
数据库连接走环境变量，默认本地 root 空密码 / pandabot 库。
"""

import os

from nonebot import on_command, on_message, get_driver
from nonebot.params import CommandArg
from nonebot.adapters.qq import MessageEvent, Message

from .store import store

driver = get_driver()


@driver.on_startup
async def _init_db():
    # NoneBot 不自动把 .env 注入 os.environ，这里手动把 DB_* 灌进去供 store 读取
    cfg = driver.config
    for key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME", "DB_SSL"):
        val = getattr(cfg, key.lower(), None)
        if val is not None and str(val) != "":
            os.environ[key] = str(val)
    await store.init()


@driver.on_shutdown
async def _close_db():
    await store.close()


# ============ 从事件取稳定的用户键 ============
def resolve_user(event: MessageEvent) -> tuple[str, str, str]:
    """返回 (存储键 session_id, 原始 openid, 昵称)。
    session_id 已含群/私聊场景，同一人换群或私聊不会串号。"""
    session_key = event.get_session_id()
    openid = event.get_user_id()
    nickname = getattr(event.author, "username", "") or "朋友"
    return session_key, openid, nickname


# ============ 全局监听：每条消息自动建档 + 计数（不回话、不阻断） ============
record = on_message(priority=99, block=False)


@record.handle()
async def _(event: MessageEvent):
    uid, openid, nickname = resolve_user(event)
    await store.touch(uid, openid, nickname)


# ============ 命令：我的信息（读取） ============
my_info = on_command("我的信息", aliases={"me", "我的资料"}, priority=10)


@my_info.handle()
async def _(event: MessageEvent):
    uid, _, nickname = resolve_user(event)

    data = await store.all(uid)
    note = data.get("note") or "（还没有记录）"
    msg_count = data.get("msg_count", 0)
    first_seen = data.get("first_seen", "—")

    await my_info.send(
        f"【{nickname}】的资料\n"
        f"首次见面：{first_seen}\n"
        f"对话次数：{msg_count}\n"
        f"备忘：{note}"
    )


# ============ 命令：记录（写入） ============
note_cmd = on_command("记录", aliases={"note", "记下"}, priority=10)


@note_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    uid, _, _ = resolve_user(event)

    content = arg.extract_plain_text().strip()
    if not content:
        await note_cmd.send("用法：记录 <你要记下的内容>")
        return

    await store.set(uid, "note", content)
    await note_cmd.send(f"已记下：{content}")


# ============ 命令：数据库状态（排查"数据到底写哪了"） ============
db_cmd = on_command("数据库状态", aliases={"数据库", "db状态"}, priority=10)


@db_cmd.handle()
async def _(event: MessageEvent):
    info = store.status()
    if info.get("kind") is None:
        await db_cmd.finish("数据库还没初始化完成，等几秒再试")

    if info["kind"] == "mysql":
        await db_cmd.finish(
            f"数据库后端：MySQL\n"
            f"目标：{info['target']}\n"
            f"连接正常，数据写在远程库里"
        )

    lines = ["数据库后端：SQLite（本地文件）", f"文件：{info['target']}"]
    if info.get("degraded"):
        lines += [
            "",
            "⚠ 本来要连 MySQL，但连接失败，已自动降级到本地",
            f"原因：{info.get('reason') or '未知'}",
            "后果：数据只存在这台机器上，搭档和服务器都看不到",
        ]
    else:
        lines.append("（DB_HOST=none，属于主动选择的本地模式）")

    await db_cmd.finish("\n".join(lines))
