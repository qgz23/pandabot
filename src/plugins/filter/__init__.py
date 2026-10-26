# -*- coding: utf-8 -*-
"""
敏感词自动撤回
================================================================
监听**全量群消息**（``GROUP_MESSAGE_CREATE``），命中词库就撤回。

前提只有一个，但很硬：机器人必须能收到群里的**每一条**消息。
官方默认只推 @ 机器人的那条，要收全量得去开放平台管理端开启
「接收所有消息」，或者让群主在手机 QQ 里把
「机器人可获取的群聊消息范围」改成「获取群内全部消息」。

这个事件的 Intent 是 ``GROUP_AND_C2C_EVENT``（bit 25），
也就是配置里已有的 ``c2c_group_at_messages``，**不需要额外加 intent**。
开关没开的话，本插件静默收不到任何消息，看起来像坏了。

另一个前提：撤回需要机器人是群管理员，而且只限消息发出后 2 分钟内。
"""

from __future__ import annotations

import time
from pathlib import Path

from nonebot import get_driver, on_command, on_message
from nonebot.adapters.qq import Bot, Message, MessageEvent
from nonebot.adapters.qq.event import GroupMessageCreateEvent
from nonebot.adapters.qq.exception import ActionFailed
from nonebot.exception import StopPropagation
from nonebot.log import logger
from nonebot.params import CommandArg

from src.plugins.settings.config import get
from src.utils.qq_api import recall_group_message

from .words import Hit, WordBook, normalize

# src/plugins/filter/__init__.py → 上溯四级是项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_WORDS_FILE = "config/banned_words.json"

book = WordBook(PROJECT_ROOT / DEFAULT_WORDS_FILE)

# 同群提示冷却：{冷却键: 上次提示的 monotonic 时间}
_last_notify: dict[str, float] = {}

# 本次启动收到多少群消息，用来判断「接收所有消息」到底开没开。
# 全量权限没开时只会推 @ 机器人的消息，plain 永远是 0。
_stats = {"total": 0, "plain": 0}


def _resolve(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _may_notify(key: str, cooldown: float) -> bool:
    """同群同类提示的冷却判断，防止刷屏。"""
    now = time.monotonic()
    if now - _last_notify.get(key, 0.0) < cooldown:
        return False
    _last_notify[key] = now
    return True


def _command_starts() -> set[str]:
    starts = getattr(get_driver().config, "command_start", None)
    return set(starts) if starts else {"/"}


def _is_command(text: str) -> bool:
    """以命令前缀开头的消息不参与过滤。

    否则管理员发「/禁言 某某」时，如果某某里带敏感词，
    命令消息本身会被撤回，命令也就没法执行了。
    """
    stripped = text.lstrip()
    return any(stripped.startswith(s) for s in _command_starts())


# ====================================================================
# 启动：定位词库、缺文件就写模板
# ====================================================================
driver = get_driver()


@driver.on_startup
async def _startup() -> None:
    book.path = _resolve(get("filter.words_file", DEFAULT_WORDS_FILE))

    if book.write_default():
        logger.info(f"敏感词过滤：已生成词库模板 {book.path}，照着往里加词就行")

    ok, msg = book.load(force=True)
    if ok:
        logger.info(f"敏感词过滤：{msg}（{book.path}）")
    else:
        logger.warning(f"敏感词过滤没读成功：{msg}（{book.path}）")


# ====================================================================
# 监听全量群消息
# ====================================================================
# priority=1 比所有命令（priority=10）都靠前，但 block=False 不拦路：
# 正常消息照旧往下走，只有命中违规时才主动中断传播。
watch = on_message(priority=1, block=False)


@watch.handle()
async def _on_group_message(bot: Bot, event: MessageEvent):
    if not get("filter.enabled", True):
        return

    # 只处理群消息。频道、私聊不在这个功能范围内。
    if not isinstance(event, GroupMessageCreateEvent):
        return

    # 机器人自己和其他机器人发的消息不处理，否则提示语可能自己撤自己
    if getattr(getattr(event, "author", None), "bot", False):
        return

    # 统计放在过滤之前：不命中的消息也要算，否则没法判断权限开没开
    _stats["total"] += 1
    if not getattr(event, "to_me", False):
        _stats["plain"] += 1

    text = event.get_message().extract_plain_text()
    if not text.strip():
        return

    if _is_command(text):
        return

    group_openid = str(getattr(event, "group_openid", "") or "")
    if not group_openid or not book.allow_group(group_openid):
        return

    book.reload_if_changed()
    hit = book.match(text)
    if hit is None:
        return

    logger.info(
        f"敏感词命中「{hit.category}」group={group_openid} "
        f"user={getattr(event.author, 'member_openid', '')} word={hit.word!r}"
    )

    if book.recall:
        if not await _recall(bot, event, group_openid, hit):
            return
        if book.notify and _may_notify(group_openid, book.cooldown):
            try:
                await watch.send(book.notify_text)
            except Exception:
                logger.debug("敏感词提示发送失败", exc_info=True)

    # 违规消息已经处理完，别让后面的插件再对它做回复
    raise StopPropagation


async def _recall(
    bot: Bot, event: MessageEvent, group_openid: str, hit: Hit
) -> bool:
    """撤回那条消息。返回是否成功。"""
    message_id = str(getattr(event, "id", "") or "")
    if not message_id:
        logger.warning("敏感词命中但拿不到消息 ID，无法撤回")
        return False

    try:
        await recall_group_message(bot, group_openid, message_id)
        return True
    except ActionFailed as e:
        if e.code == 40062003:
            # 两种可能：机器人不是管理员，或者发言人是管理员/群主
            role = getattr(getattr(event, "author", None), "member_role", "member")
            logger.warning(
                f"敏感词撤回被拒（无权限）group={group_openid} "
                f"对方角色={role} word={hit.word!r}"
            )
            if _may_notify(f"perm:{group_openid}", max(book.cooldown, 60)):
                try:
                    await watch.send(
                        "检测到违规消息，但机器人没有撤回权限。\n"
                        "请在群设置里把熊猫娘设为群管理员（群主/管理员的消息撤不了）"
                    )
                except Exception:
                    logger.debug("权限提示发送失败", exc_info=True)
        elif e.code == 40064004:
            logger.info(f"敏感词消息已超过 2 分钟，撤不回 group={group_openid}")
        else:
            logger.warning(
                f"敏感词撤回失败 code={e.code} msg={e.message} group={group_openid}"
            )
        return False
    except Exception:
        logger.exception("敏感词撤回接口异常")
        return False


# ====================================================================
# /过滤 —— 状态、测试、重载
# ====================================================================
filter_cmd = on_command("过滤", aliases={"敏感词", "词库"}, priority=10)


def _status() -> str:
    lines = [f"敏感词过滤：{'已开启' if book.enabled else '已关闭'}"]

    try:
        shown = book.path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        shown = book.path.as_posix()
    lines.append(f"词库：{shown}")
    lines.append(f"　　　{book.summary()}")

    flags = [
        "撤回 开" if book.recall else "撤回 关",
        f"提示 开（冷却 {int(book.cooldown)} 秒）" if book.notify else "提示 关",
    ]
    lines.append("动作：" + " / ".join(flags))
    lines.append(f"范围：{len(book.groups)} 个群" if book.groups else "范围：所有群")

    # 这一段是排查「为什么没反应」的关键：全量消息权限没开时，
    # 群里没人 @ 机器人，本插件就一条消息都收不到，看起来像坏了。
    total, plain = _stats["total"], _stats["plain"]
    if total == 0:
        lines.append("消息：本次启动还没收到任何群消息")
    elif plain == 0:
        lines.append(f"消息：收到 {total} 条，但全都是 @ 机器人的")
        lines.append("　　　→ 去开放平台开「接收所有消息」，否则过滤收不到消息")
    else:
        lines.append(f"消息：收到 {total} 条，其中 {plain} 条没 @ 机器人")

    if book.last_error:
        lines.append(f"注意：{book.last_error}")
    return "\n".join(lines)


@filter_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    text = arg.extract_plain_text().strip()
    book.reload_if_changed()

    if not text:
        await filter_cmd.finish(_status())

    if text in ("重载", "reload"):
        ok, msg = book.load(force=True)
        if not ok:
            await filter_cmd.finish(f"重载失败：{msg}")
        await filter_cmd.finish(f"已重载：{msg}")

    if text.startswith("测试") or text.startswith("test"):
        sample = text[2:].strip() if text.startswith("测试") else text[4:].strip()
        if not sample:
            await filter_cmd.finish("用法：/过滤 测试 一段想试试的话")

        hit = book.match(sample)
        if hit:
            await filter_cmd.finish(
                f"命中「{hit.category}」：{hit.word}\n归一化后：{_norm(sample)}"
            )
        await filter_cmd.finish(f"没命中。归一化后是：{_norm(sample)}")

    await filter_cmd.finish("用法：\n/过滤 —— 看词库状态\n/过滤 测试 一段话\n/过滤 重载")


def _norm(text: str) -> str:
    return normalize(text) or "（空）"
