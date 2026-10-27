# -*- coding: utf-8 -*-
"""
功能菜单 / 帮助
================================================================
    /帮助               完整菜单（默认出图）
    /帮助 舞萌          只看某一块的详细说明（含别名），也出图
    /help 3             也支持用序号

默认发图：1080 宽的长图，命令 + 一句话说明，手机上看着比一屏文字舒服。
settings.json 里把 menu.image 设成 false 就退回纯文字。
出图或发图的任何一步炸了都会自动回退文字 —— 菜单是用户唯一能自己找答案的地方，
不能因为画个图把命令搞哑。

排版靠「分组 + 按半角宽度对齐」。中文/全角按 2 个半角算，
否则 /今日老婆 和 /今日超能力 差一个字，空格数就会差 1 格，菜单看着是歪的。
（图片版是按像素对齐的，不受这个影响；文字版仍然需要。）

启动时会做一次自检：菜单里声明了但实际没加载成功的插件，会在日志里告警。
原因很现实——插件报错没起来时，NoneBot 只是少加载一个插件，不会主动告诉你，
而菜单看起来一切正常，用户发了没反应，你就得一个个猜。
"""

import asyncio

from nonebot import get_driver, on_command
from nonebot.adapters.qq import Message, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.plugin import get_loaded_plugins

from src.plugins.settings.config import get

from . import render

# 命令 -> 别名。只在「分组详情」里显示，总览页不显示——
# 40 多条命令全带上别名会把菜单撑到两屏，反而看不清。
# 定义在 data.py 而不是这里：那边不依赖 nonebot，渲染脚本能直接拿来测版式。
from .data import ALIASES, SECTIONS, all_plugins, item_count

# 每个分组的完整条数。resolve() 可能只返回组内一部分，
# build_section() / render_section() 靠它判断该不该标「相关 n 条」。
_SECTION_SIZE = {section: len(items) for section, items in SECTIONS}

# 文字版用法列的目标宽度，单位是「半角字符」
WIDTH_HALF = 26

# 关键词同义词：老叫法/口语 -> 标准命令名。
# 只在 resolve() 里用，不影响菜单显示。
SYNONYMS: dict[str, str] = {
    "改资料": "我的",
    "改名片": "我的",
    "抽卡": "娱乐",
    "抽签": "娱乐",
    "查分工具": "舞萌",
    "帮助文档": "帮助",
}


def _pad(usage: str) -> str:
    """按半角宽度补空格，让说明列左边缘对齐。"""
    visual = sum(2 if ord(ch) > 127 else 1 for ch in usage)
    return " " * max(2, WIDTH_HALF - visual)


def build_menu() -> str:
    """总览：所有分组 + 所有命令 + 一句话说明。"""
    lines = [
        f"熊猫娘 bot 功能菜单",
        f"共 {len(SECTIONS)} 个分组 · {item_count()} 条指令",
        "",
    ]
    for idx, (section, items) in enumerate(SECTIONS, 1):
        lines.append(f"━━━ {idx}. {section} ━━━")
        for label, usage, desc, _ in items:
            lines.append(f"{usage}{_pad(usage)}{desc}")
        lines.append("")
    lines.append("看某一块的详细说明：/帮助 舞萌")
    lines.append("按序号也行：/help 3")
    return "\n".join(lines)


def build_section(idx: int, section: str, items) -> str:
    """单组详情：命令 + 说明 + 别名。

    items 可能是 resolve() 过滤后的子集（比如 /帮助 老婆 只给你相关的 5 条），
    这时候标题标「相关 n 条」，免得用户以为这个组只剩这么点东西。
    """
    total = _SECTION_SIZE.get(section, len(items))
    if len(items) < total:
        head = f"━━━ {idx}. {section}（相关 {len(items)} 条）━━━"
    else:
        head = f"━━━ {idx}. {section}（{len(items)} 条）━━━"
    lines = [head, ""]
    for label, usage, desc, _ in items:
        lines.append(usage)
        lines.append(f"    {desc}")
        alias = ALIASES.get(label, "")
        if alias:
            lines.append(f"    ↳ 也认：{alias}")
        lines.append("")
    lines.append("看全部：/帮助")
    return "\n".join(lines)


def _names(item) -> set[str]:
    """一个菜单项被哪些写法命中：命令名、用法串、别名。"""
    label, usage = item[0], item[1]
    names = {label.lower(), usage.lower().lstrip("/")}
    names |= {
        a.strip().lower().lstrip("/")
        for a in ALIASES.get(label, "").replace("·", " ").split()
    }
    return names


def resolve(raw: str):
    """把用户输入映射到分组。

    返回 None 表示没给关键词（出总览）；
    返回空列表表示给了但没匹配上（回提示，不出菜单）。
    优先级：序号 > 命令名/别名精确 > 组名 > 模糊包含。

    注意第三段「精确命中」并没有直接整组返回：命中只用来挑**哪个组**，
    挑完还要再用关键词过滤一遍。否则 /帮助 老婆 会给出娱乐组全部 13 条
    （因为「老婆」是 /今日老婆 的别名），而用户只想要老婆相关的 5 条。
    """
    key = " ".join((raw or "").split()).lower()
    if not key:
        return None

    # 老叫法兼容：菜单早期把 /改CN 和 /改组别 合成过一条「改资料」，
    # 有人可能记着旧名字，别让他得到"没找到"
    key = SYNONYMS.get(key, key)

    if key.isdigit():
        i = int(key)
        if 1 <= i <= len(SECTIONS):
            section, items = SECTIONS[i - 1]
            return [(i, section, items)]
        return []

    def subset(items) -> tuple:
        return tuple(x for x in items if key in x[0].lower() or key in x[1].lower())

    # 精确命中：只用来确定目标组，返回内容仍按关键词收窄
    for i, (section, items) in enumerate(SECTIONS, 1):
        for item in items:
            if key in _names(item):
                return [(i, section, subset(items) or (item,))]

    # 组名
    hits = [(i, s, it) for i, (s, it) in enumerate(SECTIONS, 1) if key in s.lower()]
    if hits:
        return hits

    # 模糊包含：只给出命中的那几条，不整组糊上去
    # （/帮助 老婆 应该给 5 条带"老婆"的，而不是娱乐组全 13 条）
    hits = []
    for i, (section, items) in enumerate(SECTIONS, 1):
        sub = tuple(
            x for x in items
            if key in x[0].lower() or key in x[1].lower()
        )
        if sub:
            hits.append((i, section, sub))
    return hits


help_cmd = on_command("熊猫娘帮助", aliases={"帮助", "菜单", "help", "指令"}, priority=5)


def _dedup(groups) -> list[tuple[int, str, tuple]]:
    """同一组只留一份（resolve 的模糊匹配可能让一个组出现两次）。"""
    seen: set[str] = set()
    out: list[tuple[int, str, tuple]] = []
    for i, section, items in groups:
        if section in seen:
            continue
        seen.add(section)
        out.append((i, section, items))
    return out


def _build_text(groups) -> str:
    if groups is None:
        return build_menu()
    return "\n".join(
        build_section(i, section, items) for i, section, items in _dedup(groups)
    ).strip()


def _draw(groups) -> tuple[bytes, str]:
    """渲染帮助图，返回 (PNG 字节, 文件名)。同步函数，调用方丢线程里跑。"""
    if groups is None:
        sections = [(i, s, it) for i, (s, it) in enumerate(SECTIONS, 1)]
        return render.render_overview(sections, total=item_count()), "menu.png"

    picked = _dedup(groups)
    if len(picked) == 1:
        idx, section, items = picked[0]
        png = render.render_section(
            idx,
            section,
            items,
            total_in_section=_SECTION_SIZE.get(section, len(items)),
            aliases=ALIASES,
        )
        return png, f"menu-{idx}.png"

    # 命中多个组：套用总览版式，一张图里排完，别连发好几张刷屏
    total = sum(len(items) for _i, _s, items in picked)
    return render.render_overview(picked, total=total), "menu-hit.png"


@help_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    keyword = arg.extract_plain_text().strip()
    groups = resolve(keyword)

    # 一个都没匹配上：要让用户看组名去挑，这种场景文字比图清楚
    if groups is not None and not groups:
        names = " · ".join(s for s, _ in SECTIONS)
        await help_cmd.finish(
            f"没找到「{keyword}」相关的指令。\n\n"
            f"可以试：{names}\n"
            f"或者不带参数发 /帮助 看全部"
        )

    if get("menu.image", True):
        try:
            png, fname = await asyncio.to_thread(_draw, groups)
            await help_cmd.send(MessageSegment.file_image(png, fname))
            return
        except Exception:
            logger.exception("帮助菜单出图失败，改发文字版")

    await help_cmd.finish(_build_text(groups))


driver = get_driver()


def _plugin_short_name(p) -> str:
    """取插件短名。

    注意：NoneBot 2.5 的 Plugin 对象字段叫 `id_`（带下划线），不是 `id`。
    直接写 p.id 会 AttributeError，而且是启动期才炸——所有功能都加载完之后
    才在 on_startup 里挂掉，排查时很容易被误导成"插件有问题"。
    这里用 getattr 兜住两种写法，跨版本都不会崩。
    """
    raw = getattr(p, "id_", None) or getattr(p, "id", None) or ""
    return str(raw).rsplit(".", 1)[-1]


@driver.on_startup
async def _selfcheck() -> None:
    """对比菜单声明与实际加载的插件，找出「菜单里有、实际没起来」的功能。"""
    loaded = {_plugin_short_name(p) for p in get_loaded_plugins()}
    loaded.discard("")
    declared = all_plugins()
    missing = sorted(declared - loaded)

    if missing:
        logger.warning(
            f"菜单里这些功能对应的插件没加载成功：{', '.join(missing)}"
            "（检查插件是否报错、目录名是否对得上）"
        )
    else:
        logger.info(f"菜单自检通过：{len(declared)} 个功能插件全部在线")
