# -*- coding: utf-8 -*-
"""
图像渲染基础设施（被多个插件共用）
================================================================
放在 src/utils/ 而不是某个插件里，因为舞萌 B50 和今日老婆都要画图，
放插件里就得互相 import，插件之间直接依赖是很难维护的。

只提供"基建"：字体查找、渐变、圆角、文本排版。
具体长什么样由各插件的 render.py 决定。

**不依赖 nonebot**，可以脱离 bot 用脚本直接调用测试。
"""

from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 中文字体查找顺序：先 Windows，再 Linux（以后上服务器），最后 macOS
_REGULAR_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",     # 黑体
    "C:/Windows/Fonts/Deng.ttf",       # 等线
    "C:/Windows/Fonts/simsun.ttc",     # 宋体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)

_BOLD_FONTS = (
    "C:/Windows/Fonts/msyhbd.ttc",     # 微软雅黑 Bold
    "C:/Windows/Fonts/Dengb.ttf",      # 等线 Bold
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

Color = tuple[int, int, int] | tuple[int, int, int, int]


@lru_cache(maxsize=64)
def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """按字号取字体。结果缓存，避免每次渲染都重新解析字体文件（很慢）。

    找不到中文字体时退回 PIL 内置字体：中文会显示成方块，
    但至少不会抛异常把整个命令搞崩。
    """
    for path in (_BOLD_FONTS if bold else _REGULAR_FONTS):
        if not Path(path).exists():
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1 支持指定字号
    except TypeError:
        return ImageFont.load_default()


def vgradient(width: int, height: int, top: Color, bottom: Color) -> Image.Image:
    """竖直渐变。先画 1px 宽的细条再拉伸，比逐行画线快很多。"""
    strip = Image.new("RGB", (1, max(1, height)))
    px = strip.load()
    last = max(1, height - 1)
    for y in range(height):
        t = y / last
        px[0, y] = (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        )
    return strip.resize((width, height), Image.Resampling.BILINEAR)


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    """生成圆角蒙版，用于把矩形裁成圆角。"""
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255
    )
    return mask


def paste_rounded(
    canvas: Image.Image, layer: Image.Image, xy: tuple[int, int], radius: int
) -> None:
    """把 layer 以圆角方式贴到 canvas 上。"""
    canvas.paste(layer, xy, rounded_mask(layer.size, radius))


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(text, font=font)


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    ellipsis: str = "…",
) -> str:
    """把文本裁到指定宽度内，超长用省略号结尾。中文按像素量宽，不能按字数。"""
    if text_width(draw, text, font) <= max_width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text_width(draw, text[:mid] + ellipsis, font) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis if lo else ellipsis


# 避头尾（禁则处理）：这些字符不能出现在行首 / 行尾。
# 不处理的话「无聊。」会被折成「无聊」+「。」，右边孤零零一个句号，很难看。
_NO_LINE_START = "，。、；：？！）〕］｝〉》」』】…·%,.;:?!)]}"
_NO_LINE_END = "（〔［｛〈《「『【([{"

# 连续的数字/英文/百分号算一个"词"，不能从中间断开。
# 方舟技能里「攻击力提升至160%」被切成「至16」+「0%」，玩家会以为是两个数。
# 数字/英文/百分号算一个不可拆的整体；正负号也要算进去，
# 否则「+3000力量」会在 + 后面断开，变成行尾一个孤零零的 +
_TOKEN_CH = re.compile(r"[0-9A-Za-z%+\-]")


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    max_lines: int = 3,
) -> list[str]:
    """按像素宽度折行。中文没有空格，只能逐字累加量宽。

    两条禁则：
    1. 收尾标点不落行首、起始括号不留行尾（宁可下一行短两个字，
       也不要行首出现孤零零的「。」）
    2. 数字和英文不拆开（「160%」不能变成「16」+「0%」）
    """
    lines: list[str] = []
    current = ""
    for ch in text:
        if text_width(draw, current + ch, font) <= max_width:
            current += ch
            continue

        if not current:
            # 单字就超宽（极端情况），硬塞进去，否则死循环
            current = ch
            continue

        # 数字/英文撞到行尾：把整段词退到下一行
        if _TOKEN_CH.match(ch) and _TOKEN_CH.match(current[-1]):
            cut = len(current)
            while cut > 0 and _TOKEN_CH.match(current[cut - 1]):
                cut -= 1
            if cut > 0:  # cut == 0 说明整行都是一个词，只能硬断
                lines.append(current[:cut])
                current = current[cut:] + ch
                if len(lines) >= max_lines:
                    break
                continue

        # 避头：行首不能是收尾标点 → 把当前行最后一个字拉下来陪它
        # 避尾：行末不能是起始括号 → 同上，把括号拉到下一行
        if len(current) >= 2 and (ch in _NO_LINE_START or current[-1] in _NO_LINE_END):
            cut = len(current) - 1
            # 拉下来的「最后一个字」如果紧跟在英文/数字后面，不能只拉一个字符，
            # 否则《Net》会被拆成「《Ne」+「t》」。顺着把整个词一起带下去，
            # 中文没有这个问题（切点左边是汉字，循环不执行，行为和以前一样）。
            while cut > 0 and _TOKEN_CH.match(current[cut - 1]):
                cut -= 1
            if cut == 0:            # 整行就是一个词，只能硬拆一个字
                cut = len(current) - 1
            carry = current[cut:]
            lines.append(current[:cut])
            current = carry + ch
        else:
            lines.append(current)
            current = ch

        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines:
        consumed = sum(len(x) for x in lines)
        if consumed < len(text):
            lines[-1] = fit_text(draw, lines[-1] + "…", font, max_width)
    return lines


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Color,
    anchor: str = "la",
) -> None:
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def draw_center(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    y: float,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Color,
) -> None:
    draw.text((center_x, y), text, font=font, fill=fill, anchor="ma")


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def new_card(width: int, height: int, top: Color, bottom: Color) -> Image.Image:
    """新建一张渐变底卡片（RGB）。"""
    return vgradient(width, height, top, bottom).convert("RGB")
