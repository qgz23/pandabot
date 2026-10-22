# -*- coding: utf-8 -*-
"""
B50 成绩卡渲染（Pillow 纯绘制，不依赖浏览器）
================================================================
为什么不用 nonebot-plugin-htmlkit：
    它要拉 Playwright + Chromium（几百 MB），首次使用还要下载浏览器，
    在 Windows + Python 3.15 上容易装崩。Pillow 是纯 Python 轮子，
    出图只需几十毫秒，而且完全离线可控。

**不依赖 nonebot**，可脱离 bot 直接调用：
    from src.plugins.maimai.render import render_b50
    open("b50.png", "wb").write(render_b50(result))
"""

from __future__ import annotations

from datetime import datetime
from math import ceil

from PIL import Image, ImageDraw, ImageOps

from src.utils.render import (
    draw_text,
    fit_text,
    get_font,
    new_card,
    paste_rounded,
    text_width,
    to_png_bytes,
    vgradient,
)

# ---------- 版式常量 ----------
W = 1040
PAD = 24
HEADER_H = 132
SECTION_H = 54
ROW_H = 42
FOOTER_H = 56

# ---------- 配色 ----------
BG_TOP = (26, 27, 46)
BG_BOTTOM = (40, 31, 56)
TEXT_MAIN = (240, 240, 248)
TEXT_SUB = (158, 158, 184)
GOLD = (255, 208, 102)
PINK = (255, 138, 176)

# 难度徽章底色
DIFF_COLORS = {
    "BASIC": (104, 198, 132),
    "ADVANCED": (240, 192, 92),
    "EXPERT": (235, 106, 108),
    "MASTER": (176, 128, 240),
    "RE:MASTER": (240, 148, 198),
}

# 达成率评级颜色
RATE_COLORS = {
    "SSS+": (255, 208, 102),
    "SSS": (250, 222, 160),
    "SS+": (198, 226, 255),
    "SS": (170, 208, 248),
    "S+": (168, 220, 200),
    "S": (148, 200, 180),
    "AAA": (168, 168, 200),
}

# 列位置（右对齐锚点 = 该列的右边界）
COL_INDEX = 74
COL_TITLE = 96
COL_TITLE_W = 336
COL_BADGE = 444
COL_ACHV = 800      # 达成率右边界
COL_RATE = 886      # 评级右边界
COL_MARK = 894      # FC / FS 标记左边界
COL_RA = 1016       # 单谱 RA 右边界


def _badge(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    color: tuple[int, int, int],
    height: int = 30,
) -> float:
    """画一个圆角小徽章，返回右边界坐标。"""
    font = get_font(19, bold=True)
    w = text_width(draw, text, font) + 22
    draw.rounded_rectangle((x, y, x + w, y + height), radius=height // 2, fill=color)
    # 徽章文字用深色，和亮底色对比更清楚
    draw.text((x + w / 2, y + height / 2), text, font=font, fill=(32, 30, 44), anchor="mm")
    return x + w


def render_b50(res, *, old_top: int = 5, show_dx_score: bool = False) -> bytes:
    """把 B50Result 渲染成 PNG 字节。"""
    new_rows = list(res.b15)
    old_rows = list(res.b35)[: max(1, old_top)]
    old_total = len(res.b35)

    body_h = 0
    body_h += SECTION_H + ROW_H * max(1, len(new_rows))
    if old_rows:
        body_h += SECTION_H + ROW_H * len(old_rows)
    height = HEADER_H + body_h + FOOTER_H

    img = new_card(W, height, BG_TOP, BG_BOTTOM).convert("RGBA")

    # ---------- 第一层：半透明背景块（必须先合成，否则不透明色盖住渐变） ----------
    overlay = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    y = HEADER_H

    def _section_bg(yy: int) -> None:
        od.rounded_rectangle(
            (PAD, yy + 6, W - PAD, yy + SECTION_H - 10), radius=10, fill=(255, 255, 255, 16)
        )

    def _rows_bg(yy: int, count: int) -> None:
        for i in range(count):
            top = yy + SECTION_H + i * ROW_H
            if i % 2 == 0:
                od.rounded_rectangle(
                    (PAD, top, W - PAD, top + ROW_H - 4), radius=8, fill=(255, 255, 255, 10)
                )

    _section_bg(y)
    _rows_bg(y, max(1, len(new_rows)))
    y += SECTION_H + ROW_H * max(1, len(new_rows))
    if old_rows:
        _section_bg(y)
        _rows_bg(y, len(old_rows))

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # ---------- 第二层：顶部信息区 ----------
    f_nick = get_font(42, bold=True)
    f_user = get_font(21)
    f_label = get_font(17, bold=True)
    f_rating = get_font(56, bold=True)

    draw_text(draw, (PAD + 6, 20), "B50 战力板", f_label, PINK)
    nickname = fit_text(draw, res.nickname, f_nick, 520)
    draw_text(draw, (PAD + 6, 42), nickname, f_nick, TEXT_MAIN)

    sub = res.username
    if res.plate:
        sub += f"｜{res.plate}"
    if res.additional_rating:
        sub += f"｜段位 {res.additional_rating}"
    draw_text(draw, (PAD + 8, 96), fit_text(draw, sub, f_user, 560), f_user, TEXT_SUB)

    draw_text(draw, (W - PAD - 8, 22), "RATING", f_label, TEXT_SUB, anchor="ra")
    draw_text(draw, (W - PAD - 8, 40), str(res.rating), f_rating, GOLD, anchor="ra")

    # 头部分隔线（注意：RGBA 上直接画带 alpha 的颜色是"写入"而非"混合"，
    # 最后 convert("RGB") 会把线变成纯白，所以这里用预先混好的实色）
    draw.line((PAD, HEADER_H - 14, W - PAD, HEADER_H - 14), fill=(72, 66, 96), width=2)

    # ---------- 第三层：成绩行 ----------
    f_idx = get_font(21, bold=True)
    f_title = get_font(25)
    f_num = get_font(23)
    f_mark = get_font(18, bold=True)

    y = HEADER_H

    def _draw_section(yy: int, label: str) -> None:
        f = get_font(22, bold=True)
        bar_x = PAD + 10
        draw.rounded_rectangle((bar_x, yy + 22, bar_x + 5, yy + 42), radius=2, fill=PINK)
        draw_text(draw, (bar_x + 16, yy + 17), label, f, TEXT_MAIN)

    def _draw_rows(yy: int, rows: list, empty_text: str) -> None:
        if not rows:
            draw_text(draw, (COL_TITLE, yy + SECTION_H + 10), empty_text, f_title, TEXT_SUB)
            return
        for i, c in enumerate(rows, 1):
            top = yy + SECTION_H + (i - 1) * ROW_H
            mid = top + ROW_H / 2 - 2

            draw_text(draw, (COL_INDEX, mid), str(i), f_idx, TEXT_SUB, anchor="rm")

            title = fit_text(draw, c.title, f_title, COL_TITLE_W)
            draw_text(draw, (COL_TITLE, mid), title, f_title, TEXT_MAIN, anchor="lm")

            diff_key = c.diff_name.upper()
            _badge(
                draw,
                COL_BADGE,
                mid - 15,
                f"{c.diff_name} {c.level}",
                DIFF_COLORS.get(diff_key, (150, 150, 170)),
            )

            draw_text(draw, (COL_ACHV, mid), f"{c.achievements:.4f}%", f_num, TEXT_MAIN, anchor="rm")

            rate_color = RATE_COLORS.get(c.rate_label, TEXT_SUB)
            draw_text(draw, (COL_RATE, mid), c.rate_label, f_num, rate_color, anchor="rm")

            draw_text(draw, (COL_RA, mid), f"+{c.ra}", f_num, GOLD, anchor="rm")

            # 全连 / 同步标记：紧跟评级右侧，RA 列已经留出了间距
            if c.fc or c.fs:
                mark = "FC" if c.fc else "FS"
                if (c.fc or "").lower() == "ap":
                    mark = "AP"
                draw_text(
                    draw, (COL_MARK, mid), mark, f_mark,
                    (255, 200, 120) if mark == "AP" else (150, 210, 190), anchor="lm",
                )

    _draw_section(y, f"新曲 BEST {len(new_rows)}")
    _draw_rows(y, new_rows, "还没有新曲成绩")
    y += SECTION_H + ROW_H * max(1, len(new_rows))

    if old_rows:
        tail = "" if len(old_rows) >= old_total else f"（显示前 {len(old_rows)}/{old_total}）"
        _draw_section(y, f"旧曲 BEST {old_total}{tail}")
        _draw_rows(y, old_rows, "还没有旧曲成绩")

    # ---------- 第四层：页脚 ----------
    fy = height - FOOTER_H + 14
    draw.line((PAD, fy - 6, W - PAD, fy - 6), fill=(62, 58, 84), width=1)
    f_foot = get_font(19)
    draw_text(
        draw, (PAD + 6, fy), f"B50 合计 RA：{res.total_ra}｜数据来源：水鱼查分器", f_foot, TEXT_SUB
    )
    draw_text(
        draw, (W - PAD - 6, fy), datetime.now().strftime("%Y-%m-%d"), f_foot, TEXT_SUB, anchor="ra"
    )

    return to_png_bytes(img.convert("RGB"))


def render_help_card() -> bytes:
    """预留：以后可以把指令说明也画成图。暂时不使用。"""
    img = new_card(800, 400, BG_TOP, BG_BOTTOM)
    draw = ImageDraw.Draw(img)
    draw_text(draw, (40, 40), "舞萌查分", get_font(38, bold=True), TEXT_MAIN)
    return to_png_bytes(img)


# ====================================================================
# 「成分图」版式：50 格封面网格
# ====================================================================
# 和上面的列表式卡片相比，这版每格铺一张曲绘封面，信息密度高得多，
# 看起来就是社区里常见的那种「B50 成分图」。
#
# 代价是它依赖网络图片，所以封面一律由 covers.py 先取好再传进来，
# 这里保持**纯同步绘制**，一张图几十毫秒出完。

GW = 1120          # 网格版宽度
GM = 26            # 左右外边距
GAP = 8            # 格子间距
COLS = 5
CELL_W = (GW - GM * 2 - GAP * (COLS - 1)) // COLS   # 207
CELL_H = 104
HEADER_TOP = 14
HEADER_H = 130
GRID_TOP = HEADER_TOP + HEADER_H + 16
SECTION_BAR = 34
FOOTER_H = 56

# 难度色的深色版，用于封面缺失时的占位底
DIFF_DARK = {
    "BASIC": (28, 62, 44),
    "ADVANCED": (66, 52, 20),
    "EXPERT": (68, 30, 32),
    "MASTER": (46, 32, 70),
    "RE:MASTER": (66, 36, 54),
}


def _cell_mask(w: int, h: int) -> Image.Image:
    """格子的压暗蒙版。

    这里刻意做得**很轻**：上半几乎不压暗，让曲绘清晰可见；
    可读性靠文字黑色描边来保证，而不是把封面糊成一片深灰。
    这是社区「成分图」好看的真正原因——封面是视觉主体，文字是叠加层。
    """
    strip = Image.new("RGBA", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        # t^1.6 让上半段更透、下半段快速变深，正好托住达成率和数值行
        alpha = int(62 + 122 * (t ** 1.6))     # 62 → 184
        strip.putpixel((0, y), (10, 8, 20, alpha))
    return strip.resize((w, h), Image.Resampling.BILINEAR)


_CELL_MASK = _cell_mask(CELL_W, CELL_H)


def _shadow_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font,
    fill,
    anchor: str = "la",
    stroke: int = 2,
) -> None:
    """带黑色描边的文字。描边是封面图上白字能看清的关键。"""
    draw.text(xy, text, font=font, fill=fill, anchor=anchor,
              stroke_width=stroke, stroke_fill=(0, 0, 0))


def _draw_cell(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    score,
    cover: Image.Image | None,
) -> None:
    """画一个成绩格子：封面铺底 + 轻压暗 + 难度色条 + 五行信息。"""
    diff_key = score.diff_name.upper()
    diff_color = DIFF_COLORS.get(diff_key, (150, 150, 170))

    if cover is not None:
        # ImageOps.fit 按比例裁剪填满，直接 resize 会把封面拉变形
        patch = ImageOps.fit(cover, (CELL_W, CELL_H), Image.Resampling.LANCZOS)
        img.alpha_composite(patch.convert("RGBA"), (x, y))
    else:
        # 没有曲绘（新曲还没传图 / 首次查询时还没下完）：
        # 不画纯色块，而是难度色渐变 + 曲名首字水印。
        # 这样即使一半格子没有封面，整体看起来也像刻意的设计，而不是"图裂了"。
        base = DIFF_DARK.get(diff_key, (40, 38, 54))
        top = tuple(min(255, v + 34) for v in base)
        img.alpha_composite(vgradient(CELL_W, CELL_H, top, base).convert("RGBA"), (x, y))

        # 水印必须画在独立的透明层上再合成。
        # 直接在 RGBA 图上画 fill=(255,255,255,30) 是「写入」而不是「混合」，
        # 最后 convert("RGB") 会把它变成一块惨白的实心字。
        wm = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
        ImageDraw.Draw(wm).text(
            (CELL_W - 26, CELL_H - 30), (score.title or "?")[0],
            font=get_font(46, bold=True), fill=(255, 255, 255, 46), anchor="mm",
        )
        img.alpha_composite(wm, (x, y))

    img.alpha_composite(_CELL_MASK, (x, y))

    # 左侧难度色条
    draw.rectangle((x, y, x + 4, y + CELL_H), fill=diff_color)

    left = x + 12
    inner_w = CELL_W - 24

    # 行1：曲名（右上角留给谱面类型标记）
    f_title = get_font(14)
    _shadow_text(
        draw, (left, y + 8),
        fit_text(draw, score.title, f_title, inner_w - 30), f_title, (255, 255, 255),
    )

    # 谱面类型：和社区图一致，用 DX / SD 小字，难度靠左侧色条已经能看出来
    kind = "DX" if score.chart_type.upper().startswith("DX") else "STD"
    _shadow_text(draw, (x + CELL_W - 11, y + 8), kind, get_font(11, bold=True),
                 (214, 206, 232), anchor="ra", stroke=1)

    # 行2：达成率（信息主体，字号最大）
    f_achv = get_font(22, bold=True)
    _shadow_text(draw, (left, y + 27), f"{score.achievements:.4f}%", f_achv, (255, 255, 255))

    # 行3：定数 → 单谱 RA，右侧评级
    f_ds = get_font(13, bold=True)
    _shadow_text(draw, (left, y + 61), f"{score.ds:g} → {score.ra}", f_ds, (255, 218, 120), stroke=1)

    f_rate = get_font(14, bold=True)
    _shadow_text(
        draw, (x + CELL_W - 11, y + 60), score.rate_label, f_rate,
        RATE_COLORS.get(score.rate_label, (214, 214, 228)), anchor="ra", stroke=1,
    )

    # 行4：曲目 id（社区图的传统）+ FC/AP 标记
    f_small = get_font(10)
    _shadow_text(draw, (left, y + 84), f"#{score.song_id}", f_small, (186, 186, 204), stroke=1)

    if score.fc or score.fs:
        mark = "FC" if score.fc else "FS"
        if (score.fc or "").lower() == "ap":
            mark = "AP"
        _shadow_text(
            draw, (x + CELL_W - 11, y + 83), mark, get_font(11, bold=True),
            (255, 200, 104) if mark == "AP" else (146, 232, 206), anchor="ra", stroke=1,
        )


def _draw_section_bar(draw: ImageDraw.ImageDraw, y: int, label: str, sub: str) -> None:
    f = get_font(19, bold=True)
    draw.rounded_rectangle((GM + 4, y + 6, GM + 9, y + 26), radius=2, fill=PINK)
    draw_text(draw, (GM + 20, y + 6), label, f, TEXT_MAIN)
    draw_text(draw, (GW - GM - 4, y + 8), sub, get_font(15), TEXT_SUB, anchor="ra")


def render_b50_grid(res, covers_map: dict[int, Image.Image], *, note: str = "") -> bytes:
    """把 B50Result 渲染成 50 格封面网格图。

    covers_map: {song_id: PIL.Image}，由 covers.py 预先下载好。
                拿不到的曲目会画成难度色占位块，不会抛异常。
    note:       右下角补充说明，比如「部分曲绘缓存中」。
    """
    b15 = list(res.b15)
    b35 = list(res.b35)
    rows15 = max(1, ceil(len(b15) / COLS))
    rows35 = max(1, ceil(len(b35) / COLS)) if b35 else 0

    body_h = rows15 * CELL_H + (rows15 - 1) * GAP
    if rows35:
        body_h += SECTION_BAR + rows35 * CELL_H + (rows35 - 1) * GAP
    height = GRID_TOP + body_h + FOOTER_H

    img = new_card(GW, height, BG_TOP, BG_BOTTOM).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # ---------- 顶部玩家信息栏 ----------
    card = Image.new("RGBA", (GW - GM * 2, HEADER_H), (250, 248, 255, 240))
    paste_rounded(img, card, (GM, HEADER_TOP), 18)

    cx0 = GM + 56
    cy0 = HEADER_TOP + HEADER_H // 2
    draw.ellipse((cx0 - 36, cy0 - 36, cx0 + 36, cy0 + 36), fill=(228, 220, 244))
    initial = (res.nickname or "?")[0]
    draw.text((cx0, cy0), initial, font=get_font(34, bold=True), fill=(96, 78, 140), anchor="mm")

    f_nick = get_font(32, bold=True)
    nick = fit_text(draw, res.nickname, f_nick, 380)
    draw_text(draw, (GM + 108, HEADER_TOP + 26), nick, f_nick, (58, 48, 82))

    sub_bits = [res.username]
    if res.plate:
        sub_bits.append(res.plate)
    if res.additional_rating:
        sub_bits.append(f"段位 {res.additional_rating}")
    f_sub = get_font(15)
    draw_text(
        draw, (GM + 110, HEADER_TOP + 76),
        fit_text(draw, " · ".join(b for b in sub_bits if b), f_sub, 380), f_sub, (140, 132, 164),
    )

    # 右侧 Rating
    sum15 = sum(c.ra for c in b15)
    sum35 = sum(c.ra for c in b35)
    rx = GW - GM - 254
    draw_text(draw, (rx, HEADER_TOP + 26), "RATING", get_font(14, bold=True), (150, 142, 174))
    draw_text(draw, (rx - 2, HEADER_TOP + 44), str(res.rating), get_font(50, bold=True), (214, 138, 62))

    f_split = get_font(14)
    draw_text(
        draw, (GW - GM - 26, HEADER_TOP + 96),
        f"旧曲 {sum35} + 新曲 {sum15} = {res.total_ra}", f_split, (140, 132, 164), anchor="ra",
    )

    # ---------- 网格 ----------
    y = GRID_TOP

    def _draw_grid(yy: int, rows: list, empty_text: str) -> int:
        """画一批格子，返回这一批的底边 y。"""
        if not rows:
            draw_text(draw, (GM + 8, yy + 8), empty_text, get_font(17), TEXT_SUB)
            return yy + CELL_H
        for i, c in enumerate(rows):
            col = i % COLS
            row = i // COLS
            x = GM + col * (CELL_W + GAP)
            cy = yy + row * (CELL_H + GAP)
            _draw_cell(img, draw, x, cy, c, covers_map.get(c.song_id))
        n_rows = ceil(len(rows) / COLS)
        return yy + n_rows * (CELL_H + GAP) - GAP

    y = _draw_grid(y, b15, "还没有新曲成绩")
    y += GAP

    if b35:
        _draw_section_bar(draw, y, f"旧曲 BEST {len(b35)}", f"共 {len(b15) + len(b35)} 首")
        y += SECTION_BAR
        _draw_grid(y, b35, "还没有旧曲成绩")

    # ---------- 页脚 ----------
    fy = height - FOOTER_H + 12
    draw.line((GM, fy - 4, GW - GM, fy - 4), fill=(62, 58, 84), width=1)
    f_foot = get_font(15)
    draw_text(
        draw, (GM + 4, fy + 8),
        f"B50 合计 RA {res.total_ra} · 数据来源 水鱼查分器 · 熊猫娘bot 生成",
        f_foot, TEXT_SUB,
    )
    if note:
        draw_text(draw, (GW - GM - 4, fy + 8), note, get_font(14), (222, 186, 132), anchor="ra")

    return to_png_bytes(img.convert("RGB"))


__all__ = ["render_b50", "render_b50_grid", "render_help_card"]
