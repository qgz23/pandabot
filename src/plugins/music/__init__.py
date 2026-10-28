# -*- coding: utf-8 -*-
"""
点歌（语音播放）
================================================================
    /点歌 起风了          搜歌，搜到多首会列候选
    /点歌 2               播放候选里的第 2 首
    /点歌                 看用法

为什么能发语音：
    QQ 群机器人发富媒体要走两步 —— 先把文件传给 ``/v2/groups/{gid}/files``
    拿 ``file_info``，再用 ``msg_type=7`` 带着它发消息。
    ``nonebot-adapter-qq 1.7.3`` 已经把这两步封装进 ``send_to_group``：
    只要消息里出现 audio 段就会自动切成 msg_type=7 并先上传。
    所以这里只写 ``MessageSegment.audio(url)``，不用自己拼请求。

两种上传方式，各有各的坑：
    ``audio(url)``      让 **QQ 服务器**去下载这个地址再转存。不占本地带宽和
                        磁盘，也绕开了「二进制直传单次不能超 5MB」的限制
                        （官方软限制 20MB）。缺点是官方文档明说
                        「服务端下载资源可能因网络等原因失败」。
    ``file_audio(bytes)`` 本地下载后直接上传。稳，但**单次不能超 5MB** ——
                        实测 128kbps 的《起风了》就已经 4.97MB，
                        稍长一点的歌必超。所以只能当兜底，不能当主路径。

    默认策略 auto：先让平台去下，失败了再自己下（超 5MB 就发直链文字）。

关于时长：
    没有 ffmpeg，读不了音频头，时长是按「文件大小 ÷ 码率」估的，
    用来提示「这歌有点长」，不参与是否发送的判断。
"""

from __future__ import annotations

import asyncio
import re
import time

from nonebot import on_command
from nonebot.adapters.qq import Message, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from src.plugins.settings.config import get

from .service import Song, download, resolve, search

# 二进制直传的硬上限。官方文档写「不能超过 5M」，没说清是 5×1024×1024
# 还是 5,000,000，按大的算 —— 万一真超了平台会报错，我们还能走 URL 兜底，
# 反过来把上限卡太小，4.97MB 这种正常长度的歌就白白走了慢路径。
DIRECT_MAX = 5 * 1048576

# 上次搜索结果缓存：{群 openid: {"songs": [...], "ts": 时刻, "by": 昵称}}
# 注意不能用 event.get_session_id()：QQ 群适配器返回的是
# ``group_{群openid}_{用户openid}``，**含用户**，按它存就变成各人选各人的了。
# 群里更常见的场景是「A 点歌、B 说点第 2 首」，所以这里按群共享，
# 只把发起人记在 by 里，在候选文案上标出来。
PENDING: dict[str, dict] = {}
PENDING_TTL = 300.0

_last_call: dict[str, float] = {}
COOLDOWN_DEFAULT = 8.0

music_cmd = on_command("点歌", aliases={"听歌", "来首歌"}, priority=10)


def _session_key(event: MessageEvent) -> str:
    gid = str(getattr(event, "group_openid", "") or "")
    return gid or event.get_session_id()


def _enabled() -> bool:
    return bool(get("music.enabled", True))


def _cooldown_ok(key: str) -> float:
    """返回还需要等几秒；0 表示可以发。"""
    gap = float(get("music.cooldown_seconds", COOLDOWN_DEFAULT) or 0)
    if gap <= 0:
        return 0.0
    last = _last_call.get(key, 0.0)
    left = gap - (time.monotonic() - last)
    return max(0.0, left)


def _touch(key: str) -> None:
    _last_call[key] = time.monotonic()
    # 顺手清理，防止群多了字典无限涨
    if len(_last_call) > 500:
        cutoff = time.monotonic() - 3600
        for k in [k for k, v in _last_call.items() if v < cutoff]:
            _last_call.pop(k, None)


def _gc_pending() -> None:
    """惰性回收过期候选。群多起来之后 PENDING 只增不减就是内存泄漏。"""
    now = time.monotonic()
    for key in [k for k, v in PENDING.items() if now - v["ts"] > PENDING_TTL]:
        PENDING.pop(key, None)


def _nick(event: MessageEvent) -> str:
    author = getattr(event, "author", None)
    return str(getattr(author, "username", "") or "") or "群友"


def _help_text() -> str:
    return (
        "点歌（机器人直接发语音）\n"
        "/点歌 起风了 —— 搜歌\n"
        "/点歌 2 —— 播放候选里的第 2 首\n"
        "\n"
        "搜到一首就直接播；搜到多首会先列候选。\n"
        "候选 5 分钟内有效，过期重新搜就行。\n"
        "\n"
        "音源是网易云。版权受限的歌可能取不到\n"
        "播放地址，那就只能给你文字链接。"
    )


def _candidate_text(keyword: str, songs: list[Song], by: str = "") -> str:
    who = f"（{by} 搜的）" if by else ""
    lines = [f"「{keyword}」找到 {len(songs)} 首{who}，回复 /点歌 序号 播放："]
    for i, s in enumerate(songs, 1):
        album = f"《{s.album}》" if s.album else ""
        lines.append(f"{i}. {s.name} · {s.artist_text}{('  ' + album) if album else ''}")
    return "\n".join(lines)


def _safe_name(name: str) -> str:
    """文件名里别带 / \\ : * ? " < > | 这些，QQ 侧可能直接报错。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" ._")
    return (cleaned or "song")[:60] + ".mp3"


async def _send_voice(
    matcher, song: Song, mode: str, max_bytes: int = 20 * 1048576
) -> bool:
    """发语音。返回 True 表示语音发出去了。

    顺序是「小文件先自己下再直传，大文件才让平台去下」，理由：

    - 本地直传最可控。实测网易云 CDN 下 4.97MB 只要 0.5 秒，
      比让 QQ 服务器去抓快得多，也不受「对方能不能访问网易云」影响。
    - 但官方直传**单次不能超 5MB**，超了就只能走 URL 上传（软限制 20MB），
      那条路依赖 QQ 服务器自己下载，官方文档明说可能失败。
    """
    direct_ok = mode in ("file", "auto")
    url_ok = mode in ("url", "auto")

    # 1. 小于直传上限：自己下，自己传
    if direct_ok and (not song.size or song.size <= DIRECT_MAX):
        data = await download(song.url, limit_bytes=max_bytes)
        if data and len(data) <= DIRECT_MAX:
            try:
                await matcher.send(MessageSegment.file_audio(data, _safe_name(song.name)))
                return True
            except Exception as e:
                logger.warning(f"语音本地直传失败（{song.name}）：{e!r}")
        elif data and len(data) > DIRECT_MAX:
            logger.info(f"《{song.name}》实际 {len(data) / 1048576:.1f}MB，超过直传上限")

    # 2. 太大 / 直传失败：让平台去下（不受 5MB 限制）
    if url_ok and song.url:
        try:
            await matcher.send(MessageSegment.audio(song.url))
            return True
        except Exception as e:
            logger.warning(f"语音 URL 上传失败（{song.name}）：{e!r}")

    return False


async def _play(matcher, song: Song) -> None:
    bitrate = int(get("music.bitrate", 128) or 128)
    mode = str(get("music.upload_mode", "auto") or "auto").lower()
    max_mb = float(get("music.max_size_mb", 18) or 18)

    # 先把「点的是哪首」发出去。取直链实测要 1~13 秒不等（聚合接口不稳定），
    # 群里干等十几秒会以为 bot 挂了。时长搜索阶段就有，不用等 resolve。
    meta = []
    if song.album:
        meta.append(f"《{song.album}》")
    if song.duration_text():
        meta.append(song.duration_text())
    caption = f"{song.title()}" + ("\n" + " · ".join(meta) if meta else "")
    await matcher.send(caption)

    timeout = float(get("music.resolve_timeout", 20) or 20)
    try:
        await asyncio.wait_for(resolve(song, bitrate=bitrate), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"点歌取直链超时（{song.title()}）")
        await matcher.finish(
            f"取播放地址超过 {timeout:.0f} 秒还没回来。\n"
            "音源接口这会儿很卡，也可能是这首歌版权受限（网易云不给直链）。\n"
            "过一会儿再点，或者换一首。"
        )
    except Exception as e:
        logger.warning(f"点歌取直链失败（{song.title()}）：{e!r}")
        await matcher.finish(
            "取不到播放地址。\n"
            "网易云对不少歌只给试听、或者直接不给直链（版权限制），换一首试试。"
        )

    if max_mb > 0 and song.size and song.size > max_mb * 1048576:
        await matcher.finish(
            f"这首有 {song.size_text()}，超过 {max_mb:.0f}MB 上限，发不了。\n"
            "换首短一点的？"
        )

    if await _send_voice(matcher, song, mode, int(max_mb * 1048576) if max_mb > 0 else 20 * 1048576):
        return

    # 语音彻底发不出去，至少把直链给出去（QQ 里点开能播）
    logger.warning(f"《{song.name}》语音发送失败，改发直链")
    await matcher.finish(f"语音没发出去，直链给你：\n{song.url}")


@music_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    if not _enabled():
        return

    text = arg.extract_plain_text().strip()
    session = _session_key(event)
    _gc_pending()

    if not text or text in ("帮助", "help", "?", "？"):
        await music_cmd.finish(_help_text())

    # 纯数字 → 当作候选序号
    if text.isdigit():
        pending = PENDING.get(session)
        if not pending or time.monotonic() - pending["ts"] > PENDING_TTL:
            PENDING.pop(session, None)
            await music_cmd.finish("候选已经过期了，重新 /点歌 歌名 搜一次吧。")
        songs: list[Song] = pending["songs"]
        idx = int(text)
        if not 1 <= idx <= len(songs):
            await music_cmd.finish(f"序号要在 1~{len(songs)} 之间。")
        wait = _cooldown_ok(session)
        if wait > 0:
            await music_cmd.finish(f"慢一点，{wait:.0f} 秒后再点。")
        _touch(session)
        PENDING.pop(session, None)
        await _play(music_cmd, songs[idx - 1])
        return

    # 关键词搜索
    wait = _cooldown_ok(session)
    if wait > 0:
        await music_cmd.finish(f"慢一点，{wait:.0f} 秒后再点。")
    _touch(session)

    limit = int(get("music.search_limit", 8) or 8)
    source = str(get("music.source", "netease") or "netease")
    try:
        songs = await search(text, limit=limit, source=source)
    except Exception as e:
        logger.warning(f"点歌搜索失败：{e!r}")
        await music_cmd.finish("音源接口暂时连不上，等会儿再试试。")

    if not songs:
        await music_cmd.finish(f"没搜到「{text}」。\n换个关键词试试，或者带上歌手的名字。")

    if len(songs) == 1:
        await _play(music_cmd, songs[0])
        return

    # 多条：列候选，把结果挂到会话上等用户选
    PENDING[session] = {
        "songs": songs,
        "ts": time.monotonic(),
        "keyword": text,
        "by": _nick(event),
    }
    await music_cmd.finish(_candidate_text(text, songs, _nick(event)))
