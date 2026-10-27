# -*- coding: utf-8 -*-
"""
帮助菜单渲染
================================================================
两种版式：
    render_overview()  总览长图：每个分组一段，全部指令 + 一句话
    render_section()   单组竖图：命令 + 说明 + 别名，一样不省

尺寸按「手机满屏看」设计：1080 宽、26px 正文，缩到手机宽度大约是 9~10pt，
再小就该眯眼了。命令列固定宽度，说明从同一条竖线起，才不是参差不齐。

**别用 ↳ └ ├ 这类箭头符号**：微软雅黑没有这些字形，Pillow 画出来是方框（实测过）。
想表达「别名」就直接写中文，或者用 · 这种几乎所有中文字体都有的符号。

不 import data.py，数据全由调用方传进来 —— 渲染层只管画，
以后加一组菜单不用动这里。
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from src.utils.render import fit_text, get_font, new_card, text_width, to_png_bytes

W = 1080
PAD = 60
CMD_COL = 360       # 命令列宽（像素）。带参数的长用法要放得下，别压到说明列
ROW_H = 48          # 总览：每条占的高度
GROUP_HEAD = 34     # 组名那一行的高度
GROUP_GAP = 34      # 组与组之间的空白
HEADER_H = 200
FOOTER_H = 136

BG_TOP = (30, 33, 42)
BG_BOT = (13, 15, 19)

C_TITLE = (255, 255, 255)
C_SUB = (146, 154, 170)
C_CMD = (236, 241, 250)
C_DESC = (166, 175, 192)
C_ALIAS = (130, 140, 158)
C_FOOT = (122, 132, 150)
C_DIM = (96, 104, 120)
C_RULE = (52, 57, 68)
C_DIVIDER = (38, 42, 52)

# 分组主题色，同时也是左侧竖条的颜色
SECTION_COLORS = {
    "娱乐": (255, 122, 170),
    "小游戏": (255, 196, 92),
    "舞萌查分": (96, 196, 255),
    "我的": (120, 220, 160),
    "管理": (188, 160, 255),
}
_FALLBACK = (150, 160, 180)


def _color(section: str) -> tuple[int, int, int]:
    return SECTION_COLORS.get(section, _FALLBACK)


def _header(draw: ImageDraw.ImageDraw, title: str, sub: str) -> None:
    draw.text((PAD, 58), title, font=get_font(46, bold=True), fill=C_TITLE)
    draw.text((PAD, 124), sub, font=get_font(24), fill=C_SUB)
    draw.line([(PAD, 178), (W - PAD, 178)], fill=C_RULE, width=1)


def _group_title(
    draw: ImageDraw.ImageDraw, y: int, idx: int, section: str, note: str
) -> int:
    """画组标题（竖色条 + 序号组名 + 右侧备注），返回下一条内容的 y。"""
    col = _color(section)
    draw.rounded_rectangle((PAD + 2, y + 5, PAD + 9, y + 29), radius=3, fill=col)

    font = get_font(32, bold=True)
    title = f"{idx}. {section}"
    draw.text((PAD + 24, y), title, font=font, fill=col)

    if note:
        nx = PAD + 24 + text_width(draw, title, font) + 18
        draw.text((nx, y + 9), note, font=get_font(22), fill=C_DIM)

    return y + GROUP_HEAD + GROUP_GAP


def _overview_row(draw: ImageDraw.ImageDraw, y: int, usage: str, desc: str) -> int:
    """总览的一行：左边命令，右边说明，同一个基线。"""
    f_cmd, f_desc = get_font(26), get_font(26)

    # 命令列也可能超宽（带参数的长用法），截断而不是让它压到说明列上
    draw.text(
        (PAD + 24, y + 10),
        fit_text(draw, usage, f_cmd, CMD_COL - 40),
        font=f_cmd,
        fill=C_CMD,
    )

    desc_x = PAD + CMD_COL
    draw.text(
        (desc_x, y + 10),
        fit_text(draw, desc, f_desc, W - PAD - desc_x),
        font=f_desc,
        fill=C_DESC,
    )
    return y + ROW_H


def render_overview(sections, *, total: int, headline: str = "熊猫娘 · 功能菜单") -> bytes:
    """总览长图。

    sections 是 [(序号, 组名, items), ...]，items 每项 4 元组。
    关键词命中多组时也复用这个版式，所以序号由调用方给（保持原组号，不重排）。
    """
    height = HEADER_H + FOOTER_H
    for _idx, _section, items in sections:
        height += GROUP_HEAD + GROUP_GAP + ROW_H * len(items)

    img = new_card(W, height, BG_TOP, BG_BOT)
    draw = ImageDraw.Draw(img)

    _header(draw, headline, f"{len(sections)} 个分组 · {total} 条指令")

    y = HEADER_H
    for idx, section, items in sections:
        y = _group_title(draw, y, idx, section, f"{len(items)} 条")
        for _label, usage, desc, _plugin in items:
            y = _overview_row(draw, y, usage, desc)

    foot_y = height - FOOTER_H + 30
    draw.line([(PAD, foot_y - 16), (W - PAD, foot_y - 16)], fill=C_RULE, width=1)
    draw.text(
        (PAD, foot_y),
        "看某一组的详细说明（含别名）：/帮助 舞萌",
        font=get_font(22),
        fill=C_FOOT,
    )
    draw.text((PAD, foot_y + 34), "按序号也行：/help 3", font=get_font(22), fill=C_FOOT)
    draw.text(
        (PAD, foot_y + 68),
        "想要新功能 / 命令不好用，找群主或搭档提",
        font=get_font(22),
        fill=C_DIM,
    )
    return to_png_bytes(img)


def _detail_row_height(has_alias: bool) -> int:
    return 100 if has_alias else 76


def render_section(
    idx: int,
    section: str,
    items,
    *,
    total_in_section: int,
    aliases: dict[str, str] | None = None,
) -> bytes:
    """单组竖图。items 可能只是关键词过滤后的子集，所以要传整组条数。"""
    aliases = aliases or {}
    note = (
        f"相关 {len(items)} 条 / 共 {total_in_section} 条"
        if len(items) < total_in_section
        else f"共 {len(items)} 条"
    )

    height = 184
    for item in items:
        height += _detail_row_height(bool(aliases.get(item[0], "")))
    height += 80

    img = new_card(W, height, BG_TOP, BG_BOT)
    draw = ImageDraw.Draw(img)

    col = _color(section)
    draw.rounded_rectangle((PAD + 2, 66, PAD + 9, 118), radius=3, fill=col)
    draw.text((PAD + 24, 56), f"{idx}. {section}", font=get_font(42, bold=True), fill=col)
    draw.text((PAD + 24, 118), note, font=get_font(24), fill=C_SUB)
    draw.line([(PAD, 158), (W - PAD, 158)], fill=C_RULE, width=1)

    y = 184
    last = len(items) - 1
    for n, (label, usage, desc, _plugin) in enumerate(items):
        draw.text((PAD + 6, y), usage, font=get_font(28), fill=C_CMD)

        alias = aliases.get(label, "")
        y_desc = y + 40
        draw.text(
            (PAD + 6, y_desc),
            fit_text(draw, desc, get_font(24), W - 2 * PAD - 12),
            font=get_font(24),
            fill=C_DESC,
        )

        if alias:
            draw.text(
                (PAD + 6, y_desc + 34),
                fit_text(draw, f"别名：{alias}", get_font(22), W - 2 * PAD - 12),
                font=get_font(22),
                fill=C_ALIAS,
            )

        y += _detail_row_height(bool(alias))
        if n < last:
            draw.line([(PAD, y - 10), (W - PAD, y - 10)], fill=C_DIVIDER, width=1)

    draw.text((PAD, height - 54), "看全部：/帮助", font=get_font(22), fill=C_FOOT)
    return to_png_bytes(img)
