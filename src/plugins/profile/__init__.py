# -*- coding: utf-8 -*-
"""
个人信息名片（/信息）
================================================================
/信息                 查自己的个人信息
/信息 CN名|组别        录入 / 修改（竖线、全角竖线、空格、换行都能分隔）
/信息录入 CN名|组别    同上的显式写法

设计上就一条：**没有资料就让人录，有资料就回资料**。
用户不需要记住两套命令——查和录是同一个入口，带了参数就是录。

输出是纯文字，不出图（名片信息量太小，出图反而慢且糊）：
    CN：小明
    熊猫动漫社-美术组
    背包：（空）
    签到积分：120

数据存在 userstore 的 extra JSON 里：cn / club_group / bag。
    不新建表——一个人的资料就三个字段，为它开一张表是过度设计；
    而且 extra 是 JSON，以后加字段（QQ、特长、入社时间）不用改表结构。

背包目前没有发放途径，所以恒为「（空）」。字段先留着，
以后做积分商店/抽卡道具，往里写 [{name, count}] 就直接能显示。
"""

from nonebot import on_command
from nonebot.adapters.qq import Message, MessageEvent
from nonebot.params import CommandArg

from src.plugins.settings.config import get
from src.plugins.userstore.store import store

MAX_CN_LEN = 16
MAX_GROUP_LEN = 20

USAGE = (
    "还没有录入个人信息。\n\n"
    "发送：/信息 CN|组别\n"
    "例如：/信息 小明|美术组\n\n"
    "（竖线后面填你在社团的组别。录完之后想单独改某一项，"
    "用 /改CN 或 /改组别，不用重发整条）"
)


def _resolve(event: MessageEvent) -> tuple[str, str, str]:
    return (
        event.get_session_id(),
        event.get_user_id(),
        str(getattr(getattr(event, "author", None), "username", "") or "朋友"),
    )


def _split(raw: str) -> tuple[str, str]:
    """把「CN 组别」拆开。竖线优先，其次空白，最后整串当 CN。

    先试竖线是因为组别名里可能带空格（「美术 组」这种），竖线更明确；
    实在没有分隔符就当成只填了 CN，组别留空报错——宁可让人补，
    不要瞎猜把半句话塞进组别。
    """
    text = (raw or "").strip()
    for sep in ("|", "｜", "\n"):
        if sep in text:
            cn, group = text.split(sep, 1)
            return cn.strip(), group.strip()
    parts = text.split(None, 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return text, ""


def _bag_lines(bag) -> list[str]:
    """背包渲染。现在是空的，但格式先定好，以后有道具直接能用。"""
    limit = int(get("profile.max_bag", 20) or 20)
    if not bag:
        return ["背包：（空）"]
    lines = ["背包："]
    for item in list(bag)[:limit]:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("id") or "?")
            try:
                count = int(item.get("count") or 1)
            except (TypeError, ValueError):
                count = 1
            lines.append(f"· {name} ×{count}" if count > 1 else f"· {name}")
        else:
            lines.append(f"· {item}")
    if len(list(bag)) > limit:
        lines.append(f"……还有 {len(list(bag)) - limit} 种没显示")
    return lines


def _card(cn: str, group: str, score: int, bag) -> str:
    club = str(get("profile.club_name", "熊猫动漫社") or "熊猫动漫社")
    lines = [f"CN：{cn}", f"{club}-{group}"]
    lines += _bag_lines(bag)
    lines.append(f"签到积分：{score}")
    return "\n".join(lines)


async def _handle(cmd, event: MessageEvent, arg: Message) -> None:
    if not get("profile.enabled", True):
        await cmd.finish("个人信息功能已关闭，请联系管理员")

    uid, openid, nickname = _resolve(event)
    raw = arg.extract_plain_text().strip()

    # 先建档：MySQL 的 UPDATE 打在不存在的行上会影响 0 行，写了个寂寞
    await store.touch(uid, openid, nickname)

    # ---------- 没有参数：查 ----------
    if not raw:
        data = await store.all(uid)
        cn = str(data.get("cn") or "").strip()
        group = str(data.get("club_group") or "").strip()
        if not cn or not group:
            await cmd.finish(USAGE)
        score = await store.get_score(uid)
        await cmd.finish(_card(cn, group, score, data.get("bag")))

    # ---------- 带参数：录 ----------
    cn, group = _split(raw)
    if not cn:
        await cmd.finish("CN 不能为空。\n" + USAGE)
    if not group:
        # 只填了一半，多半是想改某一项。直接把人指到对应命令，别让他猜语法
        await cmd.finish(
            "还差组别。\n\n"
            "只想改 CN：/改CN 新名字\n"
            "只想改组别：/改组别 新组别\n"
            "两个一起改：/信息 CN|组别"
        )
    if len(cn) > MAX_CN_LEN:
        await cmd.finish(f"CN 最多 {MAX_CN_LEN} 个字，短一点更好认")
    if len(group) > MAX_GROUP_LEN:
        await cmd.finish(f"组别最多 {MAX_GROUP_LEN} 个字")

    data = await store.all(uid)
    existed = bool(str(data.get("cn") or "").strip())

    await store.set(uid, "cn", cn)
    await store.set(uid, "club_group", group)

    score = await store.get_score(uid)
    head = "资料已更新" if existed else "录入成功"
    await cmd.finish(f"{head}\n\n" + _card(cn, group, score, data.get("bag")))


# 查 / 录 共用同一个入口：带参数就是录，不带就是查
info_cmd = on_command(
    "信息", aliases={"个人信息", "名片", "我的名片"}, priority=10
)


@info_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    await _handle(info_cmd, event, arg)


# 显式写法，给记不住语法的人一个更直白的入口
entry_cmd = on_command("信息录入", aliases={"录入信息", "录入"}, priority=10)


@entry_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    await _handle(entry_cmd, event, arg)


# ====================================================================
# 单项修改：/改CN、/改组别
# ====================================================================
# 为什么不复用 /信息：改 CN 的时候人往往只想动一个字，
# 逼他重发「新CN|原组别」，组别记错了就顺手把对的改成错的。
# 单项修改要的就是"只碰这一项"。
async def _load_profile(cmd, uid: str, openid: str, nickname: str) -> dict:
    """取已录入的资料；没录过就引导去录。

    先要求录入再允许改，是为了避免出现「只有 CN、组别空着」的半成品名片——
    名片是给别人看的，缺一项就等于没有。
    """
    await store.touch(uid, openid, nickname)
    data = await store.all(uid)
    if not str(data.get("cn") or "").strip() or not str(data.get("club_group") or "").strip():
        await cmd.finish(
            "你还没录过信息，先录入再改吧。\n"
            "发送：/信息 CN|组别\n"
            "例如：/信息 小明|美术组"
        )
    return data


def _pick(raw: str) -> str:
    """清理单个字段：去掉首尾空白，把中间连续空白压成一个空格。

    一行只放一个字段，所以不能留换行；昵称里正常不会有换行，
    多半是手滑多按了回车，压平比报错更友好。
    """
    return " ".join((raw or "").split())


async def _rename(cmd, event: MessageEvent, arg: Message, field: str) -> None:
    if not get("profile.enabled", True):
        await cmd.finish("个人信息功能已关闭，请联系管理员")

    uid, openid, nickname = _resolve(event)
    raw = arg.extract_plain_text()

    if field == "cn":
        value = _pick(raw)
        if not value:
            await cmd.finish("用法：/改CN 新名字\n例如：/改CN 阿雪")
        if len(value) > MAX_CN_LEN:
            await cmd.finish(f"CN 最多 {MAX_CN_LEN} 个字，短一点更好认")
        if "|" in value or "｜" in value:
            await cmd.finish("CN 里不要带竖线，直接发名字就行")
        label = "CN"
    else:
        value = _pick(raw)
        if not value:
            await cmd.finish("用法：/改组别 新组别\n例如：/改组别 技术组")
        if len(value) > MAX_GROUP_LEN:
            await cmd.finish(f"组别最多 {MAX_GROUP_LEN} 个字")
        label = "组别"

    data = await _load_profile(cmd, uid, openid, nickname)
    old = str(data.get(field) or "").strip()

    if old == value:
        await cmd.finish(f"{label}：没有变化，还是「{value}」")

    await store.set(uid, field, value)

    cn = value if field == "cn" else str(data.get("cn") or "").strip()
    group = value if field == "club_group" else str(data.get("club_group") or "").strip()
    score = await store.get_score(uid)

    await cmd.finish(
        f"{label}：{old} → {value}\n\n" + _card(cn, group, score, data.get("bag"))
    )


rename_cn = on_command("改CN", aliases={"改cn", "修改cn", "换cn", "改名字"}, priority=10)


@rename_cn.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    await _rename(rename_cn, event, arg, "cn")


rename_group = on_command(
    "改组别", aliases={"改组", "换组别", "改部门"}, priority=10
)


@rename_group.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    await _rename(rename_group, event, arg, "club_group")
