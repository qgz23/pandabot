# -*- coding: utf-8 -*-
"""
功能菜单 / 帮助
================================================================
    /帮助               完整菜单（默认）
    /帮助 舞萌          只看某一块的详细说明（含别名）
    /help 3             也支持用序号

为什么用纯文本而不是出图：命令多的时候图片字号小、群里看不清，
文本还能直接复制命令。

排版靠「分组 + 按半角宽度对齐」。中文/全角按 2 个半角算，
否则 /今日老婆 和 /今日超能力 差一个字，空格数就会差 1 格，菜单看着是歪的。

启动时会做一次自检：菜单里声明了但实际没加载成功的插件，会在日志里告警。
原因很现实——插件报错没起来时，NoneBot 只是少加载一个插件，不会主动告诉你，
而菜单看起来一切正常，用户发了没反应，你就得一个个猜。
"""

from nonebot import get_driver, on_command
from nonebot.adapters.qq import MessageEvent
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.plugin import get_loaded_plugins
from nonebot.adapters.qq import Message

from .data import SECTIONS, all_plugins, item_count

# 每个分组的完整条数。resolve() 可能只返回组内一部分，
# build_section() 靠它判断该不该标「相关 n 条」。
_SECTION_SIZE = {section: len(items) for section, items in SECTIONS}

# 用法列的目标宽度，单位是「半角字符」
WIDTH_HALF = 26

# 命令 -> 别名。只在「分组详情」里显示，总览页不显示——
# 30 条命令全带上别名会把菜单撑到两屏，反而看不清。
ALIASES: dict[str, str] = {
    "今日老婆": "老婆 · 我的老婆",
    "今日老公": "我的老公",
    "群友老婆": "群友对象 · 抽群友",
    "上传老婆": "上传角色 · 添加老婆",
    "老婆列表": "角色列表 · 老婆池",
    "删除老婆": "删除角色 · 删老婆",
    "今日运势": "运势 · 今日签",
    "今日猪猪": "猪猪 · 今日猪",
    "今日超能力": "超能力 · 我的超能力",
    "今日doro": "doro · Doro",
    "哈基米": "hakimi · 今日哈基米",
    "猜数字": "猜数",
    "猜拳": "",
    "骰子": "roll · 掷骰",
    "招呼": "你好 · hi",
    "b50": "B50 · 查分",
    "查歌": "搜歌 · 找歌",
    "绑定舞萌": "绑舞萌 · 绑定b50",
    "解绑舞萌": "解绑b50",
    "舞萌帮助": "maimai帮助 · b50帮助",
    "信息": "个人信息 · 名片",
    "录入信息": "信息录入 · 录入",
    "改CN": "改cn · 修改cn · 换cn · 改名字",
    "改组别": "改组 · 换组别 · 改部门",
    "签到": "打卡 · checkin",
    "积分": "我的积分 · score",
    "我的信息": "me · 我的资料",
    "记录": "note · 记下",
    "帮助": "菜单 · help · 指令",
    "重载配置": "reload",
    "数据库状态": "数据库 · db状态",
}

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


@help_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    keyword = arg.extract_plain_text().strip()
    groups = resolve(keyword)

    if groups is None:
        await help_cmd.finish(build_menu())

    if not groups:
        names = " · ".join(s for s, _ in SECTIONS)
        await help_cmd.finish(
            f"没找到「{keyword}」相关的指令。\n\n"
            f"可以试：{names}\n"
            f"或者不带参数发 /帮助 看全部"
        )

    # 关键词命中多组时全给（比如「老婆」会命中娱乐组里的好几条，只出一份）
    seen: set[str] = set()
    parts: list[str] = []
    for i, section, items in groups:
        if section in seen:
            continue
        seen.add(section)
        parts.append(build_section(i, section, items))

    await help_cmd.finish("\n".join(parts).strip())


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
