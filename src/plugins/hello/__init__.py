# -*- coding: utf-8 -*-
"""
自定义回复（JSON 驱动）
================================================================
机器人说什么，不再写在代码里，而是放在：**config/replies.json**

以前是 `await hello.send("你好呀小熊猫！…")` 硬编码，加一句就得改代码、改完要重启。
现在加回复只要编辑 JSON，存盘即生效，不碰任何 .py。

用法（文件里有 _说明 照着写就行）：
    {
      "hello|你好|hi": ["你好呀{nick}！", "找我有事吗？"],
      "@":              ["叫我干嘛～"],
      "早安|早上好":     ["早上好～"]
    }

命中规则：
    整句完全一样，或者消息以触发词开头。
    所以配了「你好」，群友发「你好呀」也能接住；配了「hi」，
    不会在 "this is hi" 这种句子里胡乱命中（前缀不在句首）。
    一个字的触发词不做前缀匹配，否则「我」会吃掉大半句话。

为什么用一条 on_message 而不是给每个词注册 on_command：
    on_command 是启动时静态注册的，JSON 里新加的词要重启才生效，
    那就等于「还是得动代码」。用一条监听 + 每次现查词典，
    新增词、删词、改词全都即时生效。

QQ 群里的一个前提：
    默认权限下机器人只能收到 @ 自己的消息，所以这条监听实际上
    主要处理「@bot 你好」和「@bot」（没说话走 @ 那一组）。
    想要全量消息得在平台申请权限，代码本身不用改。
"""

from nonebot import get_driver, on_command, on_message
from nonebot.adapters.qq import MessageEvent
from nonebot.log import logger

from src.plugins.settings.config import get

from .replies import book

driver = get_driver()

# 回复文件位置交给配置决定，改 settings.json 就能换地方，不用动代码
book.set_path_provider(lambda: get("hello.replies_file", "") or None)

# 和 COMMAND_START 对齐：发 /你好 也当「你好」处理
_STARTERS = "/／"


def _text(event: MessageEvent) -> str:
    """取纯文本，并去掉开头的命令前缀。"""
    try:
        raw = event.get_plaintext() or ""
    except Exception:
        return ""
    text = raw.strip()
    while text and text[0] in _STARTERS:
        text = text[1:].strip()
    return text


def _nick(event: MessageEvent) -> str:
    author = getattr(event, "author", None)
    return str(getattr(author, "username", "") or "") or "你"


@driver.on_startup
async def _load_reply_book() -> None:
    if book.write_default():
        logger.info(f"自定义回复：已生成模板 {book.path}，照着改就行")
    ok, msg = book.load(force=True)
    if ok:
        logger.info(f"自定义回复：{msg}（{book.path}）")
    else:
        logger.warning(f"自定义回复没有加载：{msg}（文件：{book.path}）")


# ====================================================================
# 回复监听：优先级压到最后，block=False，绝不抢命令的活
# ====================================================================
reply_any = on_message(priority=99, block=False)


@reply_any.handle()
async def _(event: MessageEvent):
    if not get("hello.enabled", True):
        return

    text = _text(event)
    # 只@了机器人、没说话 —— 走 @ 那一组回复
    at_only = (not text) and bool(getattr(event, "to_me", False))
    if not text and not at_only:
        return

    # 文件被改过就先重读，改坏了保留旧词典继续用，不会突然失声
    book.ensure_fresh()

    hit = book.match(text, at_only=at_only)
    if not hit:
        return

    await reply_any.send(book.render(hit, nick=_nick(event)))


# ====================================================================
# /重载回复：改了 JSON 想立刻确认生效，不用等发消息触发
# ====================================================================
reload_cmd = on_command("重载回复", aliases={"重载自定义回复", "重读回复"}, priority=10)


@reload_cmd.handle()
async def _():
    ok, msg = book.load(force=True)
    if ok:
        await reload_cmd.finish(f"{msg}\n文件：{book.path}")
    await reload_cmd.finish(f"没读成功，先沿用旧内容。\n{msg}\n文件：{book.path}")
