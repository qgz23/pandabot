# -*- coding: utf-8 -*-
"""
每日玩法通用卡片渲染
================================================================
五个玩法（运势/猪猪/超能力/Doro/哈基米）共用一套版式，靠主题色区分。

为什么做成「一个模板 + 主题表」而不是每个玩法一套渲染：
    娱乐向卡片的视觉结构本来就是一样的（标题 → 大字结果 → 若干字段 → 一句话），
    复制五遍只会让改版时改五处、漏四处。新增玩法在这里加一行主题色即可。

**不依赖 nonebot**，可脱离 bot 直接调用测试。
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from src.utils.render import (
    draw_text,
    fit_text,
    get_font,
    new_card,
    paste_rounded,
    text_width,
    to_png_bytes,
    wrap_text,
)

W = 780
PAD = 52
PANEL = 20
PANEL_RADIUS = 28

PANEL_WHITE = (255, 252, 254)
TEXT_DARK = (68, 62, 82)
TEXT_SUB = (150, 144, 166)
HAIRLINE = (238, 232, 244)

# 每个玩法一套配色：(渐变起, 渐变止, 主色, 浅色)
THEMES = {
    "fortune": ((255, 214, 224), (206, 196, 246), (228, 100, 144), (255, 232, 240)),
    "pig": ((255, 232, 198), (246, 200, 208), (214, 122, 88), (255, 242, 228)),
    "power": ((198, 226, 255), (206, 238, 246), (56, 118, 194), (232, 243, 255)),
    "doro": ((255, 242, 188), (250, 212, 168), (196, 144, 52), (255, 249, 226)),
    "hakimi": ((216, 242, 214), (196, 228, 240), (66, 146, 116), (236, 250, 240)),
}
DEFAULT_THEME = THEMES["fortune"]


def _theme(kind: str):
    return THEMES.get(kind, DEFAULT_THEME)


def render_daily(r) -> bytes:
    """把 DailyResult 渲染成 PNG 字节。高度按内容自动算，不用写死。"""
    bg_top, bg_bottom, accent, soft = _theme(r.kind)

    f_title = get_font(24, bold=True)
    f_date = get_font(18)
    f_head = get_font(56, bold=True)
    f_sub = get_font(22)
    f_label = get_font(20)
    f_value = get_font(21, bold=True)
    f_line = get_font(21)
    f_foot = get_font(17)

    # 先用一张临时图画布做文本测量，算出真实高度
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    inner_w = W - PAD * 2

    head_text = fit_text(probe, r.headline, f_head, inner_w)
    sub_lines = wrap_text(probe, r.sub, f_sub, inner_w - 40, max_lines=3) if r.sub else []
    line_lines = wrap_text(probe, r.line, f_line, inner_w - 60, max_lines=2) if r.line else []

    # 高度按内容算：大字固定占 86，说明按实际折行数长，字段每行 62
    y_head = 128
    y_sub = y_head + 86
    y_fields = y_sub + len(sub_lines) * 34 + (24 if sub_lines else 0)
    y_line = y_fields + len(r.fields) * 62 + 22
    H = y_line + len(line_lines) * 34 + 78
    H = max(H, 520)

    img = new_card(W, H, bg_top, bg_bottom).convert("RGBA")

    # 白色圆角面板
    panel = Image.new("RGBA", (W - PANEL * 2, H - PANEL * 2), PANEL_WHITE + (255,))
    paste_rounded(img, panel, (PANEL, PANEL), PANEL_RADIUS)

    draw = ImageDraw.Draw(img)
    cx = W // 2

    # 左下 / 右下装饰圆：只在底部空白处，绝不压文字（右上会被日期占掉）
    deco = soft + (255,)
    draw.ellipse((46, H - 100, 78, H - 68), fill=deco)
    draw.ellipse((W - 92, H - 108, W - 54, H - 70), fill=deco)

    # 标题 + 日期
    draw_text(draw, (PAD + 8, 48), r.title, f_title, accent)
    draw_text(draw, (W - PAD - 8, 54), r.date_str, f_date, (138, 132, 154), anchor="ra")

    # 顶部细线
    draw.line((PAD + 8, 92, W - PAD - 8, 92), fill=HAIRLINE, width=1)

    # 中间大字：用 "mm" 垂直居中锚点，不用担心不同字号的实际行高差异
    draw_text(draw, (cx, y_head + 34), head_text, f_head, accent, anchor="mm")

    # 大字下方说明
    y = y_sub
    for ln in sub_lines:
        draw_text(draw, (cx, y), ln, f_sub, TEXT_SUB, anchor="ma")
        y += 34

    # 字段区：左标签右值，中间用极浅的细线分隔
    y = y_fields
    for f in r.fields:
        mid = y + 26
        label_x = PAD + 8
        if f.color:
            # 幸运色这种：在标签前画一个真实色块，比写 RGB 值直观得多
            draw.rounded_rectangle(
                (label_x, mid - 9, label_x + 18, mid + 9), radius=4, fill=f.color + (255,)
            )
            label_x += 28
        draw_text(draw, (label_x, mid), f.label, f_label, TEXT_SUB, anchor="lm")

        avail = W - PAD - 8 - (label_x + text_width(draw, f.label, f_label) + 16)
        value = fit_text(draw, f.value, f_value, max(80, avail))
        draw_text(draw, (W - PAD - 8, mid), value, f_value, TEXT_DARK, anchor="rm")

        if f is not r.fields[-1]:
            draw.line((PAD + 8, y + 56, W - PAD - 8, y + 56), fill=HAIRLINE, width=1)
        y += 62

    # 底部一句话
    if line_lines:
        y = y_line
        for ln in line_lines:
            draw_text(draw, (cx, y), ln, f_line, (112, 104, 128), anchor="ma")
            y += 34

    # 页脚
    draw.line((PAD + 8, H - 62, W - PAD - 8, H - 62), fill=HAIRLINE, width=1)
    draw_text(draw, (cx, H - 44), r.footer, f_foot, TEXT_SUB, anchor="ma")

    return to_png_bytes(img.convert("RGB"))


__all__ = ["render_daily"]
