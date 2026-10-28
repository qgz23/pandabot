# -*- coding: utf-8 -*-
"""
WS 卡片图
================================================================
纯 Pillow 手绘，1040 宽，高度随效果文本长度浮动。

版式：左边原卡图，右边卡面数据，效果文本通栏放底下。

    ┌──────────────────────────────────────────────┐
    │ ┌──────────┐  《Nega Nebulus》                │
    │ │          │  Kuroyukihime                   │
    │ │          │  Gaw/WS02-E002            [RR]  │
    │ │   卡图   │  ────────────────────────────   │
    │ │  392×548 │  ● 黄  Character                 │
    │ │          │  ┌──────┬──────┬──────┐         │
    │ │          │  │ 等级 │ 费用 │ 力量 │         │
    │ │          │  │  1   │  0   │ 3000 │         │
    │ └──────────┘  └──────┴──────┴──────┘         │
    │               魂 1   触发 [图标]              │
    │ ──────────────────────────────────────────── │
    │ 【永】 If all of your characters are…        │
    └──────────────────────────────────────────────┘

三个设计决定：

1. 主题色跟着卡片颜色走（黄/绿/红/蓝）。WS 的颜色本身就是卡组的核心
   信息，一眼看底色比看文字快。整张卡的渐变、描边、数字全部取同一个色。

2. 触发不翻译成中文，直接贴官方图标。WS 的触发有十几种（魂/门/宝/风/袋/
   书/选择/回归……），靠记忆写中文名很容易错，而官方 partimages 里就有
   现成的 gif。贴图零风险，而且图标本身就是设计过的，比文字好看。

3. 卡图是 392×548 的竖版（WS 卡实测 400×558，等比缩一点留描边）。
   卡图下载失败不能崩：画一个占位块，右边信息照常显示 ——
   查卡功能的主要价值在数据，图只是锦上添花。
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from src.utils.render import (
    fit_text,
    get_font,
    new_card,
    rounded_mask,
    to_png_bytes,
    wrap_text,
)

from .service import Card

# 画布
WIDTH = 1040
PAD = 40
CARD_W = 392
CARD_H = 548
GAP = 34
INFO_X = PAD + CARD_W + GAP
INFO_W = WIDTH - INFO_X - PAD

# 文本区
TEXT_W = WIDTH - PAD * 2 - 20
TEXT_SIZE = 24
TEXT_LH = 36

# 深色底
CARD_DARK = (16, 19, 26)
PANEL_BG = (32, 39, 52)
TEXT_MAIN = (240, 244, 250)
TEXT_DIM = (156, 166, 184)
TEXT_FAINT = (112, 122, 140)

# 卡片颜色 → 主题色。名字来自官方 partimages（yellow/green/red/blue）
_COLOR_RGB = {
    "yellow": (224, 186, 68),
    "green": (76, 178, 110),
    "red": (216, 82, 82),
    "blue": (78, 144, 216),
    "purple": (154, 110, 206),
}
_COLOR_CN = {
    "yellow": "黄",
    "green": "绿",
    "red": "红",
    "blue": "蓝",
    "purple": "紫",
}
_ACCENT_FALLBACK = (132, 144, 166)

# 卡类型：官方英文站给的是 Character / Event / Climax
_KIND_CN = {
    "character": "角色卡",
    "event": "事件卡",
    "climax": "高潮卡",
}


def _kind_cn(kind: str) -> str:
    if not kind:
        return "—"
    return _KIND_CN.get(kind.strip().lower(), kind)

_trig_cache: dict[str, Image.Image] = {}


def _accent(colors: list[str]) -> tuple[int, int, int]:
    for c in colors:
        if c in _COLOR_RGB:
            return _COLOR_RGB[c]
    return _ACCENT_FALLBACK


def _mix(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _color_cn(colors: list[str]) -> str:
    names = [_COLOR_CN.get(c, c) for c in colors]
    return "/".join(names) if names else "无色"


def _trig_icon(raw: bytes | None, size: int = 48) -> Image.Image | None:
    """触发图标放大到指定尺寸。

    原图是给网页用的 24×24 gif，放大用 NEAREST 会变成一堆马赛克方块，
    看不清图案；LANCZOS 虽然边缘软一点，但图案能认出来。
    """
    if not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None
    return im.resize((size, size), Image.Resampling.LANCZOS)


def _fit_card(raw: bytes | None) -> Image.Image | None:
    """等比缩进 CARD_W×CARD_H 的框，不裁不拉。

    WS 的卡图**横竖两种都有**：普通卡 346×484 是竖的，
    高潮卡 489×350 是横的。所以外面不能画一个固定竖框把图塞进去，
    否则横图缩完只占半高，底下空一大块 —— 版式高度必须跟着实际图走。
    """
    if not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None
    if im.width < 20 or im.height < 20:
        return None
    scale = min(CARD_W / im.width, CARD_H / im.height)
    size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
    return im.resize(size, Image.Resampling.LANCZOS)


def _wrap_paragraphs(draw, text: str, font, width: int, max_lines: int) -> list[str]:
    """先按原始换行分段，每段再按像素折行。

    直接把整段丢给 wrap_text 会把原有的段落换行吃掉，
    效果文本里「【自】…\\n【起】…」的层次就没了。
    """
    lines: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if len(lines) >= max_lines:
            break
        got = wrap_text(draw, para, font, width, max_lines=max_lines - len(lines))
        # wrap_text 会在末尾字符含空格，行首也可能是空格，清掉
        lines.extend(line.strip() for line in got)
    return lines[:max_lines]


def _stat_panel(draw, x: int, y: int, w: int, h: int, items: list[tuple[str, str]],
                accent) -> None:
    """三栏数值面板：等级 / 费用 / 力量。"""
    draw.rounded_rectangle((x, y, x + w, y + h), radius=14, fill=PANEL_BG)
    n = len(items)
    if not n:
        return
    col = w / n
    label_font = get_font(19)
    value_font = get_font(34, bold=True)
    for i, (label, value) in enumerate(items):
        cx = x + col * i + col / 2
        draw.text((cx, y + 20), label, font=label_font, fill=TEXT_FAINT, anchor="ma")
        draw.text((cx, y + 48), value, font=value_font, fill=accent, anchor="ma")


def render_card(
    card: Card,
    img: bytes | None,
    icons: dict[str, bytes],
    name_zh: str = "",
    text_zh: str = "",
) -> bytes:
    """画一张卡。

    name_zh / text_zh 是翻译结果，留空就用英文原文。
    卡名翻译经常只翻对一半（人名多为音译），所以译名和原名一起显示：
    大字给中文，小灰字给英文原名，用户能自己对照。
    """
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    name_font = get_font(36, bold=True)
    sub_name_font = get_font(20)
    no_font = get_font(23)
    rarity_font = get_font(21, bold=True)
    kind_font = get_font(25)
    small_font = get_font(21)
    soul_font = get_font(28, bold=True)
    text_font = get_font(TEXT_SIZE)

    accent = _accent(card.colors)
    art = _fit_card(img)

    # ---- 先量尺寸，再开画布（高度必须跟着内容走）----
    main_name = name_zh or card.name or card.no
    # 副名就是英文原名；跟主名一样时没必要重复显示
    sub_name = card.name if (name_zh and card.name.strip() != name_zh.strip()) else ""
    body = text_zh or card.text

    name_lines = wrap_text(probe, main_name, name_font, INFO_W, max_lines=3)
    sub_lines = wrap_text(probe, sub_name, sub_name_font, INFO_W, max_lines=2) if sub_name else []
    text_lines = _wrap_paragraphs(probe, body, text_font, TEXT_W, 40) if body else []

    info_h = len(name_lines) * 46           # 卡名
    info_h += len(sub_lines) * 28 + 6       # 英文原名
    info_h += 10
    info_h += 34 + 18                       # 卡号 + 稀有度
    info_h += 24                            # 分隔线
    info_h += 46                            # 颜色 + 卡类型
    if card.is_climax:
        info_h += 62                        # 触发图标行（高潮卡没有等级费用力量）
    else:
        info_h += 116 + 20                  # 数值面板
        info_h += 46                        # 魂 / 触发

    # 左边卡图的高度决定整张卡的高度：竖版卡图撑满，横版（高潮卡）不硬撑，
    # 否则横图下面会空出一大块
    art_h = art.height if art else 300
    card_block_h = max(art_h, info_h, 300)

    text_block_h = (24 + len(text_lines) * TEXT_LH + 6) if text_lines else 0
    height = PAD + card_block_h + text_block_h + 32

    canvas = new_card(
        WIDTH,
        height,
        _mix(CARD_DARK, accent, 0.16),
        _mix(CARD_DARK, accent, 0.03),
    )
    draw = ImageDraw.Draw(canvas)

    # ---- 左：卡图（垂直居中于信息区高度内）----
    if art:
        ox = PAD + (CARD_W - art.width) // 2
        oy = PAD + (card_block_h - art.height) // 2
        canvas.paste(art, (ox, oy), rounded_mask(art.size, 12))
        draw.rounded_rectangle(
            (ox, oy, ox + art.width - 1, oy + art.height - 1),
            radius=12, outline=accent, width=2,
        )
    else:
        bh = min(CARD_H, card_block_h)
        draw.rounded_rectangle(
            (PAD, PAD, PAD + CARD_W, PAD + bh),
            radius=12, fill=PANEL_BG, outline=_mix(PANEL_BG, accent, 0.5), width=2,
        )
        draw.text(
            (PAD + CARD_W // 2, PAD + bh // 2),
            "卡图暂缺", font=get_font(26), fill=TEXT_FAINT, anchor="mm",
        )

    # ---- 右：卡面数据 ----
    x = INFO_X
    y = PAD

    for line in name_lines:
        draw.text((x, y), line, font=name_font, fill=TEXT_MAIN, anchor="la")
        y += 46
    for line in sub_lines:
        draw.text((x, y), line, font=sub_name_font, fill=TEXT_FAINT, anchor="la")
        y += 28
    y += 10

    # 卡号 + 稀有度徽章
    badge_w = int(probe.textlength(card.rarity, font=rarity_font)) + 28 if card.rarity else 0
    no_room = INFO_W - badge_w - 16 if badge_w else INFO_W
    draw.text((x, y), fit_text(probe, card.no, no_font, no_room), font=no_font,
              fill=TEXT_DIM, anchor="la")
    if badge_w:
        bx = x + INFO_W - badge_w
        draw.rounded_rectangle((bx, y, bx + badge_w, y + 34), radius=8,
                               fill=_mix(CARD_DARK, accent, 0.42))
        draw.text((bx + badge_w / 2, y + 17), card.rarity, font=rarity_font,
                  fill=TEXT_MAIN, anchor="mm")
    y += 34 + 18

    draw.line((x, y, x + INFO_W, y), fill=_mix(CARD_DARK, accent, 0.32), width=2)
    y += 24

    # 颜色点 + 卡类型
    dot_r = 9
    cy = y + 15
    for i, c in enumerate(card.colors or ["none"]):
        cx = x + dot_r + i * 26
        draw.ellipse((cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r),
                     fill=_COLOR_RGB.get(c, _ACCENT_FALLBACK))
    kind_x = x + (len(card.colors) * 26 if card.colors else 0)
    draw.text((kind_x, y), f"{_color_cn(card.colors)}  {_kind_cn(card.kind)}",
              font=kind_font, fill=TEXT_MAIN, anchor="la")
    y += 46

    if card.is_climax:
        # 高潮卡没有等级/费用/力量，画那个面板只会得到三个横杠。
        # 高潮卡真正要看的是触发（魂触发 / 回手触发……），直接放大图标。
        mid = y + 26
        draw.text((x, mid), "触发", font=small_font, fill=TEXT_FAINT, anchor="lm")
        ix = x + 64
        drawn = False
        for name in card.triggers:
            icon = _trig_icon(icons.get(name), 52)
            if icon:
                canvas.paste(icon, (ix, y), icon)
                ix += 62
                drawn = True
        if not drawn:
            label = "／".join(card.triggers) if card.triggers else "无"
            draw.text((ix, mid), label, font=small_font, fill=TEXT_DIM, anchor="lm")
    else:
        _stat_panel(
            draw, x, y, INFO_W, 116,
            [("等级", card.level or "—"),
             ("费用", card.cost or "—"),
             ("力量", card.power or "—")],
            accent,
        )
        y += 116 + 20

        # 魂 / 触发：左右两栏，不然挤在左边右边留一大片空
        line_y = y + 16
        draw.text((x, line_y), "魂", font=small_font, fill=TEXT_FAINT, anchor="lm")
        draw.text((x + 40, line_y), str(card.soul or "—"), font=soul_font,
                  fill=TEXT_MAIN, anchor="lm")

        tx = x + INFO_W // 2
        draw.text((tx, line_y), "触发", font=small_font, fill=TEXT_FAINT, anchor="lm")
        ix = tx + 60
        drawn = False
        for name in card.triggers:
            icon = _trig_icon(icons.get(name), 36)
            if icon:
                canvas.paste(icon, (ix, line_y - 18), icon)
                ix += 42
                drawn = True
        if not drawn:
            label = "／".join(card.triggers) if card.triggers else "无"
            draw.text((ix, line_y), label, font=small_font, fill=TEXT_DIM, anchor="lm")

    # ---- 底：效果文本 ----
    if text_lines:
        ty = PAD + card_block_h + 12
        draw.line((PAD, ty, WIDTH - PAD, ty), fill=_mix(CARD_DARK, accent, 0.32), width=2)
        ty += 16
        for line in text_lines:
            draw.text((PAD, ty), line, font=text_font, fill=TEXT_MAIN, anchor="la")
            ty += TEXT_LH

    return to_png_bytes(canvas)


def render_candidates(query: str, cards: list[Card]) -> bytes:
    """一次搜出好几张卡时的候选列表。

    只列卡号 + 卡名 + 稀有度，不画图 —— 这一步要快，
    用户看到列表后再用完整卡号查详情。
    """
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_font = get_font(32, bold=True)
    no_font = get_font(23)
    name_font = get_font(25)
    hint_font = get_font(20)

    row_h = 74
    height = PAD + 56 + len(cards) * row_h + 60 + PAD
    accent = _ACCENT_FALLBACK
    canvas = new_card(WIDTH, height, _mix(CARD_DARK, accent, 0.12), _mix(CARD_DARK, accent, 0.02))
    draw = ImageDraw.Draw(canvas)

    draw.text((PAD, PAD), fit_text(probe, f"「{query}」找到 {len(cards)} 张", title_font, TEXT_W),
              font=title_font, fill=TEXT_MAIN, anchor="la")
    y = PAD + 56

    for card in cards:
        if y + row_h > height - PAD - 40:
            break
        draw.rounded_rectangle((PAD, y, WIDTH - PAD, y + row_h - 10), radius=12, fill=PANEL_BG)
        accent_c = _accent(card.colors)
        draw.rounded_rectangle((PAD, y, PAD + 6, y + row_h - 10), radius=3, fill=accent_c)

        draw.text((PAD + 24, y + 12), fit_text(probe, card.no, no_font, 290),
                  font=no_font, fill=TEXT_FAINT, anchor="la")
        room = WIDTH - PAD * 2 - 24 - 300 - 60
        draw.text((PAD + 310, y + 9), fit_text(probe, card.name, name_font, room),
                  font=name_font, fill=TEXT_MAIN, anchor="la")
        if card.rarity:
            # 稀有度用提亮后的主题色：直接用主题色在深底上对比度太低，看不清
            draw.text((WIDTH - PAD - 16, y + 12), card.rarity, font=no_font,
                      fill=_mix(accent_c, TEXT_MAIN, 0.55), anchor="ra")
        y += row_h

    draw.text((PAD, height - PAD - 32),
              "发 /ws 加完整卡号看详情", font=hint_font, fill=TEXT_FAINT, anchor="la")
    return to_png_bytes(canvas)
