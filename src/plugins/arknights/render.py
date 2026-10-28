# -*- coding: utf-8 -*-
"""
干员图鉴卡片
================================================================
纯 Pillow 手绘，1000 宽，高度随内容浮动。

版式是「左图右信息的档案卡」：
    ┌───────────────────────────────────────┐
    │ ┌────────┐  阿米娅                    │
    │ │        │  Amiya                     │
    │ │  立绘   │  ★★★★★                 │
    │ │        │  中坚术师 · 远程位           │
    │ │        │  罗德岛  #输出  一句话简介    │
    │ └────────┘                            │
    │ 特性   攻击造成法术伤害                  │
    │ 天赋   情绪吸收 额外回复技力 …           │
    │ 技能   ▣ 战术咏唱·γ型  攻击速度+90       │
    │        ▣ 精神爆发  8 连发，晕眩 10 秒    │
    │ 属性   生命 1480 攻击 612 防御 121 …     │
    └───────────────────────────────────────┘

几个必须踩对的点：

1. 立绘是 1024×1024 的**透明底** PNG，人物只占中间一小块。
   直接缩放到框里，人会小一半。所以先用 getbbox() 裁掉透明边，
   再等比缩放到框内 —— 这样尺寸不一的干员看起来才一样大。

2. 高度不能写死。有的干员 3 个天赋、有的 0 个，技能描述长短也差很多。
   所以先量好每一块的折行结果，累加出总高，再开画布。
   （写死高度的结果是：长内容被裁掉，短内容底下留一大片空白。）

3. 星级用 ★/☆ 字符而不是画星星多边形。字体里就有，缩放清晰，
   少写几十行三角函数。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from src.utils.render import (
    draw_text,
    fit_text,
    get_font,
    new_card,
    rounded_mask,
    to_png_bytes,
    wrap_text,
)

from .mapping import POSITION_CN, RARITY_COLORS, profession_cn
from .service import Char

# 画布
WIDTH = 1000
PAD = 40
LIVE_BOX = 400          # 立绘框边长
COL_GAP = 36            # 立绘与右栏的间距
PANEL_BG = (30, 36, 48)
TEXT_MAIN = (242, 245, 250)
TEXT_DIM = (154, 164, 182)
TEXT_FAINT = (110, 120, 138)
CARD_TOP = (14, 17, 23)
CARD_BOTTOM = (24, 29, 40)

# 右栏可用宽度
RIGHT_X = PAD + LIVE_BOX + COL_GAP
RIGHT_W = WIDTH - RIGHT_X - PAD

# 技能块的图尺寸
ICON_SIZE = 52
SKILL_ICON_X = PAD + 96
SKILL_TEXT_X = SKILL_ICON_X + ICON_SIZE + 16
SKILL_TEXT_W = WIDTH - PAD - SKILL_TEXT_X


def _fit_icon(raw: bytes | None) -> Image.Image | None:
    """技能图标：128×128 原图缩到 52，四个角切圆。

    图标本身是透明底 + 白色描边图形，直接贴到深色底上是干净的，
    不需要额外加背景板。
    """
    if not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None
    im = im.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
    # 圆角和图标原本的透明通道要相乘，不能直接 putalpha —
    # 直接盖会把图形外的透明区域涂成不透明，变成一块黑方底
    im.putalpha(ImageChops.multiply(im.getchannel("A"), rounded_mask(im.size, 10)))
    return im


def _fit_portrait(raw: bytes | None) -> Image.Image | None:
    """把原始立绘裁掉透明边、等比缩放进立绘框。"""
    if not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGBA")
    except Exception:
        return None

    bbox = im.getbbox()  # 透明底图用这个裁掉四周空白
    if bbox:
        im = im.crop(bbox)
    if im.width < 8 or im.height < 8:
        return None

    scale = min(LIVE_BOX / im.width, LIVE_BOX / im.height)
    size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
    return im.resize(size, Image.Resampling.LANCZOS)


def _chips(draw, texts: list[str], font, gap: int = 10, pad_x: int = 14,
           max_width: int = RIGHT_W) -> tuple[list[tuple[str, int, int]], int]:
    """把标签排成流式小圆角块，返回 [(文字, 宽, 行号)] 和总行数。

    手写流式布局而不用第三方库：一共就几个标签，
    引个排版库不值当，而且字体度量还得自己算。
    """
    placed: list[tuple[str, int, int]] = []
    row = 0
    cursor = 0
    for text in texts:
        w = int(draw.textlength(text, font=font)) + pad_x * 2
        if cursor and cursor + w > max_width:
            row += 1
            cursor = 0
        placed.append((text, w, row))
        cursor += w + gap
    return placed, row + 1 if placed else 0


def render_card(char: Char, portrait: bytes | None,
                icons: dict[str, bytes] | None = None,
                sub_names: dict[str, str] | None = None) -> bytes:
    """画一张干员卡，返回 PNG 字节。

    icons: {技能 id: 图标字节}。缺哪个就那个技能画占位方块，
           不影响整张卡 —— 图标是锦上添花，不能因为它拿不到就报错。
    """
    accent = RARITY_COLORS.get(char.rarity, TEXT_DIM)
    icons = icons or {}

    # 用一个 1×1 的画布当"尺子"，先量文字宽度。绘制阶段还要用，留着。
    ruler = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    f_name = get_font(54, bold=True)
    f_en = get_font(22)
    f_star = get_font(26)
    f_meta = get_font(25)
    f_chip = get_font(19)
    f_usage = get_font(20)
    f_head = get_font(21, bold=True)
    f_body = get_font(21)
    f_val = get_font(25, bold=True)

    # ---------------- 右栏预排版 ----------------
    name = fit_text(ruler, char.name, f_name, RIGHT_W)
    en = fit_text(ruler, char.en.upper(), f_en, RIGHT_W) if char.en else ""
    prof_cn = profession_cn(char.prof, char.sub, sub_names)
    pos_cn = POSITION_CN.get(char.position, "")
    meta_line = " · ".join(x for x in (prof_cn, pos_cn) if x)

    chips, chip_rows = _chips(ruler, [f"#{t}" for t in char.tags[:4]], f_chip)

    # 一句话简介。放右栏而不是正文区：右栏本来只有 5 行字，
    # 立绘框却有 400 高，不加点东西右边会空一大块
    usage_lines = wrap_text(ruler, char.usage, f_chip, RIGHT_W, max_lines=3) if char.usage else []

    # ---------------- 正文块预排版 ----------------
    body_w = WIDTH - PAD * 2 - 96  # 左边留出「特性」「天赋」这种小标题的位置
    blocks: list[tuple[str, list[tuple[str, str]]]] = []

    if char.trait:
        lines = wrap_text(ruler, char.trait, f_body, body_w, max_lines=3)
        blocks.append(("特性", [("", line) for line in lines]))

    talent_rows: list[tuple[str, str]] = []
    for talent in char.talents[:3]:
        # 天赋名 + 描述放同一行开头，读起来更省空间
        desc_lines = wrap_text(ruler, talent["desc"], f_body, body_w, max_lines=3)
        talent_rows.append((talent["name"], desc_lines[0] if desc_lines else ""))
        for extra in desc_lines[1:]:
            talent_rows.append(("", extra))
    if talent_rows:
        blocks.append(("天赋", talent_rows))

    # 技能块单独排版：一个技能 = 左边图标 + 右边名字和效果，不是一个字符串列表
    f_sk_name = get_font(21, bold=True)
    f_sk_desc = get_font(19)
    skill_rows: list[tuple[dict, list[str], int]] = []
    for skill in char.skills[:3]:
        desc_lines = wrap_text(ruler, skill.get("d", ""), f_sk_desc, SKILL_TEXT_W, max_lines=3)
        row_h = max(ICON_SIZE, 28 + len(desc_lines) * 27)
        skill_rows.append((skill, desc_lines, row_h))

    # 属性：两行四列
    st = char.stats
    stat_items = [
        ("生命", st.get("hp", 0)),
        ("攻击", st.get("atk", 0)),
        ("防御", st.get("def", 0)),
        ("法抗", st.get("res", 0)),
        ("部署", st.get("cost", 0)),
        ("阻挡", st.get("block", 0)),
        ("间隔", f"{st.get('interval', 0)}s"),
    ]
    stat_text = "   ".join(f"{label} {value}" for label, value in stat_items)

    # 右栏内容高度（名字/英文/星级/职业/阵营/标签/简介）
    right_h = (64 + (34 if en else 0) + 40 + 40 + 34
               + chip_rows * 40 + 10
               + (len(usage_lines) * 30 + 14 if usage_lines else 0))
    head_h = max(LIVE_BOX, right_h)

    # 正文块高度
    line_h = 36
    block_gap = 26
    body_h = 0
    for _, rows in blocks:
        body_h += 32 + len(rows) * line_h + block_gap
    if skill_rows:
        body_h += 32 + sum(row_h + 10 for _s, _l, row_h in skill_rows) + block_gap
    body_h += 32 + 40 + block_gap  # 属性块

    total_h = PAD + head_h + 34 + body_h + PAD + 26

    # ---------------- 开画 ----------------
    canvas = new_card(WIDTH, total_h, CARD_TOP, CARD_BOTTOM)
    draw = ImageDraw.Draw(canvas)

    # 顶部一条星级色，卡片有"头"不会显得飘
    draw.rectangle((0, 0, WIDTH, 5), fill=accent)

    # 立绘：先铺一个圆角展示台，再贴人
    panel = Image.new("RGB", (LIVE_BOX, LIVE_BOX), PANEL_BG)
    canvas.paste(panel, (PAD, PAD), rounded_mask(panel.size, 28))

    live = _fit_portrait(portrait)
    if live:
        # 人物底边贴着展示台底部，视觉上"站"在框里
        px = PAD + (LIVE_BOX - live.width) // 2
        py = PAD + LIVE_BOX - live.height
        canvas.paste(live, (px, py), live)
    else:
        draw.text((PAD + LIVE_BOX // 2, PAD + LIVE_BOX // 2), "暂无立绘",
                  font=f_meta, fill=TEXT_FAINT, anchor="mm")

    # 立绘框描边
    draw.rounded_rectangle((PAD, PAD, PAD + LIVE_BOX, PAD + LIVE_BOX),
                           radius=28, outline=(48, 56, 72), width=2)

    # 右栏
    y = PAD + 12
    draw_text(draw, (RIGHT_X, y), name, f_name, TEXT_MAIN)
    y += 64
    if en:
        draw_text(draw, (RIGHT_X, y), en, f_en, TEXT_FAINT)
        y += 34

    stars = "★" * char.rarity + "☆" * (6 - char.rarity)
    draw_text(draw, (RIGHT_X, y), stars, f_star, accent)
    y += 40
    draw_text(draw, (RIGHT_X, y), meta_line, f_meta, TEXT_MAIN)
    y += 40
    draw_text(draw, (RIGHT_X, y), char.faction, f_chip, TEXT_DIM)
    y += 34

    # 标签
    cursor_x = RIGHT_X
    current_row = 0
    for text, w, row in chips:
        if row != current_row:
            cursor_x = RIGHT_X
            current_row = row
            y += 40
        box = (cursor_x, y, cursor_x + w, y + 32)
        draw.rounded_rectangle(box, radius=16, fill=(38, 45, 60))
        draw.text((cursor_x + w // 2, y + 16), text, font=f_chip,
                  fill=TEXT_DIM, anchor="mm")
        cursor_x += w + 10

    # 简介（标签下方）
    if usage_lines:
        y += 40
        for line in usage_lines:
            draw.text((RIGHT_X, y), line, font=f_usage, fill=TEXT_DIM)
            y += 30

    # 分隔线
    sep_y = PAD + head_h + 18
    draw.line((PAD, sep_y, WIDTH - PAD, sep_y), fill=(46, 53, 68), width=1)

    # 正文
    y = sep_y + 26
    for title, rows in blocks:
        draw.text((PAD, y + 2), title, font=f_head, fill=accent)
        ty = y
        for name_part, text in rows:
            if name_part:
                tw = draw.textlength(name_part, font=f_body)
                draw.text((PAD + 96, ty), name_part, font=f_body, fill=TEXT_MAIN)
                if text:
                    draw.text((PAD + 96 + tw + 12, ty), text, font=f_body, fill=TEXT_DIM)
            else:
                draw.text((PAD + 96, ty), text, font=f_body, fill=TEXT_DIM)
            ty += line_h
        y += 32 + len(rows) * line_h + block_gap

    # 技能：一个技能一行 —— 左图标 + 右名字和效果
    if skill_rows:
        draw.text((PAD, y + 2), "技能", font=f_head, fill=accent)
        sy = y
        for skill, desc_lines, row_h in skill_rows:
            icon = _fit_icon(icons.get(skill.get("i", "")))
            # 描述长的时候行高会超过图标，图标垂直居中看着才不飘
            iy = sy + (row_h - ICON_SIZE) // 2
            if icon:
                canvas.paste(icon, (SKILL_ICON_X, iy), icon)
            else:
                # 图标拿不到就画个方块占位，里面放技能名首字 ——
                # 比空白或问号有信息量，也不至于让版式塌掉
                draw.rounded_rectangle(
                    (SKILL_ICON_X, iy, SKILL_ICON_X + ICON_SIZE, iy + ICON_SIZE),
                    radius=10, fill=(38, 45, 60),
                )
                draw.text((SKILL_ICON_X + ICON_SIZE // 2, iy + ICON_SIZE // 2),
                          (skill.get("n") or "技")[0],
                          font=f_sk_name, fill=TEXT_FAINT, anchor="mm")
            draw.text((SKILL_TEXT_X, sy - 2), skill.get("n", ""),
                      font=f_sk_name, fill=TEXT_MAIN)
            ty = sy + 28
            for line in desc_lines:
                draw.text((SKILL_TEXT_X, ty), line, font=f_sk_desc, fill=TEXT_DIM)
                ty += 27
            sy += row_h + 10
        y += 32 + sum(row_h + 10 for _s, _l, row_h in skill_rows) + block_gap

    # 属性
    draw.text((PAD, y + 2), "属性", font=f_head, fill=accent)
    draw.text((PAD + 96, y), stat_text, font=f_val, fill=TEXT_MAIN)
    y += 40
    lv = char.stats.get("level")
    elite = char.stats.get("elite", 0)
    elite_cn = {0: "初始", 1: "精一", 2: "精二", 3: "精三"}.get(elite, f"{elite}阶段")
    note = f"{elite_cn} Lv{lv}" if lv else ""
    if char.obtain:
        note = f"{note} · 获取：{char.obtain}" if note else f"获取：{char.obtain}"
    draw.text((PAD + 96, y + 4), fit_text(draw, note, f_chip, WIDTH - PAD * 2 - 96),
              font=f_chip, fill=TEXT_FAINT)

    # 底部水印（顺手交代属性口径，不然「生命 2560」是哪一级的没人知道）
    draw.text((WIDTH - PAD, total_h - 26),
              "数据 ArknightsGameData · 立绘 Aceship · 技能为专精三数值",
              font=get_font(17), fill=(72, 80, 96), anchor="rs")

    return to_png_bytes(canvas)


def render_candidates(query: str, chars: list[Char]) -> bytes:
    """搜到多个干员时的候选列表图（比刷一屏文字好看）。"""
    ruler = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    f_title = get_font(34, bold=True)
    f_name = get_font(26, bold=True)
    f_meta = get_font(19)

    row_h = 62
    height = PAD * 2 + 70 + len(chars) * row_h + 36
    canvas = new_card(WIDTH, height, CARD_TOP, CARD_BOTTOM)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, WIDTH, 5), fill=(255, 138, 61))

    draw.text((PAD, PAD), f"找到 {len(chars)} 名干员", font=f_title, fill=TEXT_MAIN)
    draw.text((PAD, PAD + 46), f"关键词「{query}」", font=f_meta, fill=TEXT_FAINT)

    y = PAD + 92
    for char in chars:
        accent = RARITY_COLORS.get(char.rarity, TEXT_DIM)
        draw.rounded_rectangle((PAD, y, WIDTH - PAD, y + row_h - 10),
                               radius=12, fill=(26, 32, 44))
        # 左侧星级色竖条，一眼能看出稀有度
        draw.rounded_rectangle((PAD, y, PAD + 6, y + row_h - 10), radius=3, fill=accent)
        draw.text((PAD + 24, y + (row_h - 10) // 2), char.name,
                  font=f_name, fill=TEXT_MAIN, anchor="lm")
        stars = "★" * char.rarity
        prof = profession_cn(char.prof, char.sub)
        meta = f"{stars}  {prof} · {char.faction}"
        draw.text((WIDTH - PAD - 24, y + (row_h - 10) // 2), meta,
                  font=f_meta, fill=TEXT_DIM, anchor="rm")
        y += row_h

    draw.text((PAD, height - 28), "发 /干员 名字 看详情", font=f_meta, fill=TEXT_FAINT)
    return to_png_bytes(canvas)
