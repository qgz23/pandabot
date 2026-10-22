# -*- coding: utf-8 -*-
"""
今日老婆（群友上传角色版）
================================================================
/上传老婆 <名字>|<台词>   带一张图上传角色，成功 +5 积分
/删除老婆 <名字>          删掉自己上传的该角色所有图
/老婆列表                 看本群有哪些角色、各有多少张图
/今日老婆                 从本群角色池抽一个
/今日老公                 同一套玩法，换一个签池（池子共用）

数据模型：**一次上传 = 一条记录**（名字 + 图 + 台词 + 上传者）。
同一个角色名可以被多人上传不同图，抽到该角色时从它的图池里随机一张，
连带那张图对应的台词。

**输出形式：原图 + 一条文字**，不再合成卡片。
    图：原图原样发出（不裁不压不改构图），只有超过 QQ 发送上限时才等比压缩。
    字：角色名 + 台词 + 上传者署名。
为什么不合成一张卡：合成必然要缩图——卡片宽 700 出头，手机拍的原图放进去
就等于强制降采样。用户要的是"看这张图"，不是"看一张带边框的小图"。
图和字分两条发，画质和排版各归各的，谁也不将就谁。

设计要点：
    - **确定性抽签**：同一天 + 同一个群 + 同一个人，角色和图都不变，第二天自动换。
    - **图片必须当场下载**：QQ 消息里的图片 url 有时效，存 url 等于存了个明天就失效的链接。
    - 上传积分走 store.add_score()，写 users.score 列；
      千万别用 store.incr(uid, "score", n)，那是写进 extra JSON，/积分 读不到。

配置在 settings.json 的 "waifu" 段，改完约 3 秒热生效：
    upload_reward        每次上传奖励积分（默认 5）
    daily_reward_limit   每人每天最多拿几次上传奖励（默认 5，防刷分）
"""

from datetime import date

from nonebot import get_driver, on_command
from nonebot.adapters.qq import Message, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from src.plugins.settings.config import get
from src.plugins.userstore.store import store

from .roles import RoleError, roles
from .service import draw, to_text

driver = get_driver()

# 上传奖励的每日计数：{uid: (日期, 今天已领次数)}
# 只放内存，重启就清零——刷分的人不会为了多几分去重启 bot
_reward_log: dict[str, tuple[str, int]] = {}


def _group_openid(event: MessageEvent) -> str:
    return str(getattr(event, "group_openid", "") or "")


def _nickname(event: MessageEvent) -> str:
    author = getattr(event, "author", None)
    return str(getattr(author, "username", "") or "") or "群友"


def _image_urls(event: MessageEvent) -> list[str]:
    """从消息里挑出图片地址。QQ 收到的图片段是 data={"url": ...}，
    这个 url 是临时的，必须马上下载。"""
    urls: list[str] = []
    try:
        message = event.get_message()
    except Exception:
        return urls
    for seg in message:
        if getattr(seg, "type", "") != "image":
            continue
        data = getattr(seg, "data", {}) or {}
        url = str(data.get("url") or data.get("file") or "")
        if url.startswith("http"):
            urls.append(url)
    return urls


def _split_name_line(raw: str) -> tuple[str, str]:
    """把 "名字|台词" 拆开。竖线、全角竖线、换行都认。"""
    raw = (raw or "").strip()
    for sep in ("|", "｜", "\n"):
        if sep in raw:
            name, line = raw.split(sep, 1)
            return name.strip(), line.strip()
    return raw, ""


def _take_reward(uid: str) -> bool:
    """今天还能不能拿上传奖励。"""
    try:
        limit = int(get("waifu.daily_reward_limit", 5))
    except (TypeError, ValueError):
        limit = 5
    if limit <= 0:
        return False

    today = date.today().isoformat()
    day, used = _reward_log.get(uid, (today, 0))
    if day != today:
        used = 0
    if used >= limit:
        _reward_log[uid] = (today, used)
        return False

    if len(_reward_log) > 5000:
        for k in [k for k, v in _reward_log.items() if v[0] != today]:
            _reward_log.pop(k, None)

    _reward_log[uid] = (today, used + 1)
    return True


USAGE = (
    "用法：/上传老婆 角色名|角色台词 + 一张图\n"
    "例如：/上传老婆 雪之下雪乃|我不需要朋友，但你可以。\n"
    "（名字和台词用竖线隔开，台词会跟在图片下面发出来）"
)


# ====================================================================
# /上传老婆
# ====================================================================
upload_cmd = on_command("上传老婆", aliases={"上传角色", "添加老婆"}, priority=10)


@upload_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    if not get("waifu.enabled", True):
        await upload_cmd.finish("今日老婆功能已关闭，请联系管理员")

    group = _group_openid(event)
    if not group:
        await upload_cmd.finish("这个玩法要在群里用哦～")

    name, line = _split_name_line(arg.extract_plain_text())
    urls = _image_urls(event)

    if not urls:
        await upload_cmd.finish("要带一张图片才行。\n" + USAGE)
    if not name:
        await upload_cmd.finish(USAGE)
    if not line:
        # 台词是这个玩法的核心之一，缺了就跟普通图片池没区别，所以要求补齐
        await upload_cmd.finish("还差这个角色的出场台词。\n" + USAGE)

    uid = event.get_session_id()
    openid = event.get_user_id()
    nick = _nickname(event)

    # 先建档，add_score 才能 UPDATE 到行（否则影响 0 行，白给分）
    try:
        await store.touch(uid, openid, nick)
    except Exception:
        logger.debug("touch 失败，不影响上传", exc_info=True)

    try:
        data = await roles.download(urls[0])
        role = await roles.add(
            group, name, data, line=line, uploader=openid, uploader_name=nick
        )
    except RoleError as e:
        await upload_cmd.finish(str(e))
    except Exception:
        logger.exception("上传角色失败")
        await upload_cmd.finish("上传失败了，稍后再试试")

    # 积分奖励
    reward_text = ""
    try:
        gain = int(get("waifu.upload_reward", 5))
    except (TypeError, ValueError):
        gain = 5

    if gain > 0:
        if _take_reward(uid):
            try:
                total = await store.add_score(uid, gain)
                reward_text = f"\n积分 +{gain}，你现在有 {total} 分"
            except Exception:
                logger.exception("上传奖励积分失败")
                reward_text = "\n（积分发放失败，请联系管理员）"
        else:
            reward_text = "\n今天的上传奖励已经拿满了，但角色照样收录～"

    pool = await roles.list_roles(group)
    same = sum(1 for r in pool if r.name == role.name)
    distinct = len({r.name for r in pool})
    extra = f"，「{role.name}」现在有 {same} 张图" if same > 1 else ""

    await upload_cmd.finish(
        f"已收录「{role.name}」{extra}\n"
        f"本群共 {distinct} 个角色 / {len(pool)} 张图{reward_text}"
    )


# ====================================================================
# /删除老婆
# ====================================================================
remove_cmd = on_command("删除老婆", aliases={"删除角色", "删老婆"}, priority=10)


@remove_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    if not get("waifu.enabled", True):
        await remove_cmd.finish("今日老婆功能已关闭，请联系管理员")

    group = _group_openid(event)
    if not group:
        await remove_cmd.finish("这个玩法要在群里用哦～")

    keyword = arg.extract_plain_text().strip()
    if not keyword:
        await remove_cmd.finish("要删哪个？\n用法：/删除老婆 角色名（只会删掉你自己传的）")

    try:
        count, name = await roles.remove(group, keyword, requester=event.get_user_id())
    except RoleError as e:
        await remove_cmd.finish(str(e))

    await remove_cmd.finish(f"已删除「{name}」你上传的 {count} 张图")


# ====================================================================
# /老婆列表
# ====================================================================
list_cmd = on_command("老婆列表", aliases={"角色列表", "老婆池"}, priority=10)


@list_cmd.handle()
async def _(event: MessageEvent):
    if not get("waifu.enabled", True):
        await list_cmd.finish("今日老婆功能已关闭，请联系管理员")

    group = _group_openid(event)
    if not group:
        await list_cmd.finish("这个玩法要在群里用哦～")

    pool = await roles.list_roles(group)
    if not pool:
        await list_cmd.finish(
            "本群还没有角色。\n"
            "用 /上传老婆 角色名|台词 + 一张图 就能加进来，上传还能拿积分～"
        )

    # 按角色名聚合，同名的多张图算一个角色
    buckets: dict[str, list] = {}
    for r in pool:
        buckets.setdefault(r.name, []).append(r)
    names = sorted(buckets)

    show = names[:30]
    lines = [f"本群角色池：{len(names)} 个角色 · {len(pool)} 张图"]
    for i, name in enumerate(show, 1):
        items = buckets[name]
        who = "、".join(sorted({x.by_name for x in items if x.by_name}))
        tail = f"（{len(items)} 张" + (f" · {who}" if who else "") + "）"
        lines.append(f"{i}. {name}{tail}")
    if len(names) > len(show):
        lines.append(f"……还有 {len(names) - len(show)} 个没显示")
    lines.append("发 /今日老婆 抽一个")
    await list_cmd.finish("\n".join(lines))


# ====================================================================
# /今日老婆  /今日老公
# ====================================================================
async def _play(cmd, event: MessageEvent, salt: str) -> None:
    if not get("waifu.enabled", True):
        await cmd.finish("今日老婆功能已关闭，请联系管理员")

    group = _group_openid(event)
    if not group:
        await cmd.finish("这个玩法要在群里用哦～")

    pool = await roles.list_roles(group)
    result = draw(
        date_str=date.today().isoformat(),
        group_openid=group,
        querier_openid=event.get_user_id(),
        roles=pool,
        salt=salt,
    )

    if result is None:
        await cmd.finish(
            "本群还没有角色，抽不出来。\n"
            "用 /上传老婆 角色名|台词 + 一张图 加一个，上传还能拿积分～"
        )

    photo = await roles.send_ready(group, result.image) if result.image else None

    # 先发原图，再发文字。两件事分开：图就是图，字就是字。
    if photo:
        try:
            await cmd.send(MessageSegment.file_image(photo, result.image or "wife.jpg"))
        except Exception:
            # 图发不出去也别让整条命令哑掉，文字照发
            logger.exception("今日老婆原图发送失败，只发文字")

    await cmd.finish(to_text(result))


wife_cmd = on_command("今日老婆", aliases={"老婆", "我的老婆"}, priority=10)


@wife_cmd.handle()
async def _(event: MessageEvent):
    await _play(wife_cmd, event, salt="wife")


husband_cmd = on_command("今日老公", aliases={"我的老公"}, priority=10)


@husband_cmd.handle()
async def _(event: MessageEvent):
    await _play(husband_cmd, event, salt="husband")


@driver.on_shutdown
async def _close_roles() -> None:
    await roles.aclose()
