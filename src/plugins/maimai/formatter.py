# -*- coding: utf-8 -*-
"""
舞萌查分的消息格式化层：数据对象 → QQ 消息文本。
================================================================
本模块 **不 import 任何 nonebot 相关代码**，纯字符串处理，方便单独调样式。
想改输出长相（加表情、换顺序、改成表格）只改这个文件，不动业务逻辑。
"""

from __future__ import annotations

from .service import B50Result, ChartScore, SongHit

# 一行里曲名最长保留多少字，超了截断，防止某首歌名超长把整行撑爆
TITLE_MAX = 22

# 水鱼回的 fc / fs 是缩写，这里翻成玩家看得懂的标记
_FC_MARKS = {"fc": "FC", "fcp": "FC+", "ap": "AP"}
_FS_MARKS = {"fs": "FS", "fsp": "FS+", "sync": "SYNC"}


def _short(title: str) -> str:
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1] + "…"


def _pct(v: float) -> str:
    return f"{v:.4f}%"


def format_score_line(index: int, c: ChartScore) -> str:
    """单条成绩：` 1. 曲名 [MASTER 14+] 100.9876% SSS+ +330 FC`"""
    mark = ""
    if c.fc:
        mark = " " + _FC_MARKS.get(c.fc.lower(), c.fc.upper())
    elif c.fs:
        mark = " " + _FS_MARKS.get(c.fs.lower(), c.fs.upper())
    return (
        f"{index:>2}. {_short(c.title)} "
        f"[{c.diff_name} {c.level}] "
        f"{_pct(c.achievements)} {c.rate_label} +{c.ra}{mark}"
    )


def format_b50(res: B50Result, *, old_top: int = 5) -> str:
    """B50 结果 → 文本。新曲 15 条全列，旧曲只列前 old_top 条控制长度。"""
    head = f"🐼 {res.nickname}｜Rating {res.rating}"
    if res.additional_rating:
        head += f"｜段位 {res.additional_rating}"
    if res.plate:
        head += f"｜{res.plate}"

    lines: list[str] = [head, ""]

    if res.b15:
        lines.append(f"◆ 新曲 BEST {len(res.b15)}")
        lines.extend(format_score_line(i, c) for i, c in enumerate(res.b15, 1))

    if res.b35:
        show = res.b35[: max(1, old_top)]
        lines.append("")
        lines.append(f"◆ 旧曲 BEST {len(res.b35)}（显示前 {len(show)}）")
        lines.extend(format_score_line(i, c) for i, c in enumerate(show, 1))

    if not res.b15 and not res.b35:
        lines.append("查分器里还没有成绩记录，先去水鱼查分器上传成绩吧～")

    lines.append("")
    lines.append(f"B50 合计 RA：{res.total_ra}｜数据来源：水鱼查分器")
    return "\n".join(lines)


def format_search(hits: list[SongHit], keyword: str) -> str:
    """查歌结果 → 文本。一条一行，避免刷屏。"""
    if not hits:
        return f"没找到包含「{keyword}」的曲目，换个关键词试试？"

    lines = [f"🔍 「{keyword}」找到 {len(hits)} 条（最多显示 10 条）"]
    for s in hits:
        ds = "/".join(f"{d:g}" for d in s.ds) or "-"
        lines.append(f"[{s.id}] {_short(s.title)} ({s.chart_type}) 定数 {ds}")
    lines.append("")
    lines.append("定数顺序：BASIC / ADVANCED / EXPERT / MASTER（有第 5 个是 Re:MASTER）")
    return "\n".join(lines)


def format_bind_ok(res: B50Result) -> str:
    return (
        f"绑定成功！\n"
        f"查分器昵称：{res.nickname}\n"
        f"用户名：{res.username}\n"
        f"当前 Rating：{res.rating}"
        + (f"｜段位 {res.additional_rating}" if res.additional_rating else "")
        + "\n\n现在直接发 /b50 就能查自己的成绩了。"
    )


def format_help() -> str:
    return (
        "🐼 舞萌查分指令\n"
        "────────────\n"
        "/绑定舞萌 <水鱼用户名>　绑定账号（会先真查一遍验证）\n"
        "/解绑舞萌　　　　　　　清除绑定\n"
        "/b50　　　　　　　　　 查自己的 B50\n"
        "/b50 <用户名>　　　　　 查别人的 B50\n"
        "/查歌 <关键词>　　　　　搜曲目、看定数\n"
        "/舞萌帮助　　　　　　　 就是这条\n"
        "────────────\n"
        "数据来自水鱼查分器，需要对方先上传过成绩；"
        "没绑定过就先 /绑定舞萌 你的水鱼用户名。"
    )
