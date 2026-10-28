# -*- coding: utf-8 -*-
"""
中文映射表
================================================================
character_table.json 里除了「名字」和「标签」，其余全是英文枚举：
    profession   = "CASTER"
    nationId     = "rhodes"
    position     = "RANGED"

游戏里的中文名不在任何 gamedata 文件里（鹰角把本地化文本和枚举值分开放了），
只能自己维护一份。这里只放**八大职业 / 阵营 / 位置**这种稳定不动的，
数量少、永不过时。

子职业（subProfessionId）故意不在这里硬编码：
72 个子职业的中文名（"攻城手"/"散射手"…）靠记忆写必错一批，
错了还看不出来。所以卡片上显示「职业 + 特性」——
特性描述本身就是子职业的定义（"同时攻击阻挡的所有敌人"），
玩家一看就懂，而且这句话是数据里直接拿的，不会错。

真想显示子职业名，往 data/arknights/subprof_names.json 里填，
格式 {"siegesniper": "攻城手"}，填了就用，没填就退回大职业名。
"""

from __future__ import annotations

# 八大职业。键是 character_table 里的 profession 值。
PROFESSION_CN: dict[str, str] = {
    "PIONEER": "先锋",
    "WARRIOR": "近卫",
    "TANK": "重装",
    "SNIPER": "狙击",
    "CASTER": "术师",
    "MEDIC": "医疗",
    "SUPPORT": "辅助",
    "SPECIAL": "特种",
}

# 只有这八个算「可查干员」。数据表里另外 900 多条是 TRAP（装置）、
# TOKEN（召唤物）、NONE（剧情专用），名字都是"陷阱"、"无人机"这种，
# 混进搜索结果会把正经干员挤下去。
PLAYABLE_PROFESSIONS = frozenset(PROFESSION_CN)

POSITION_CN: dict[str, str] = {
    "MELEE": "近战位",
    "RANGED": "远程位",
}

# 阵营（nationId）：干员的"国籍"
NATION_CN: dict[str, str] = {
    "rhodes": "罗德岛",
    "lungmen": "龙门",
    "ursus": "乌萨斯",
    "kazimierz": "卡西米尔",
    "columbia": "哥伦比亚",
    "sargon": "萨尔贡",
    "siracusa": "叙拉古",
    "laterano": "拉特兰",
    "higashi": "东国",
    "leithanien": "莱塔尼亚",
    "victoria": "维多利亚",
    "yan": "炎",
    "sami": "萨米",
    "iberia": "伊比利亚",
    "bolivar": "玻利瓦尔",
    "rim": "雷姆必拓",
    "kjerag": "谢拉格",
    "minos": "米诺斯",
    "egir": "阿戈尔",
}

# 组织（groupId）：比国籍更能说明"这人是哪个单位的"
GROUP_CN: dict[str, str] = {
    "abyssal": "深海猎人",
    "babel": "巴别塔",
    "blacksteel": "黑钢国际",
    "elite": "使徒",
    "glasgow": "格拉斯哥帮",
    "karlan": "喀兰贸易",
    "lgd": "龙门近卫局",
    "penguin": "企鹅物流",
    "pinus": "松",
    "rhine": "莱茵生命",
    "siesta": "汐斯塔",
    "sui": "岁",
    "sweep": "S.W.E.E.P",
    "tara": "塔拉",
}

# 星级配色。方舟的星级和稀有度是同一件事，卡片主色跟着星级走，
# 六星金橙、五星明黄、四星紫、三星蓝 —— 和游戏里抽卡光效一致。
RARITY_COLORS: dict[int, tuple[int, int, int]] = {
    1: (154, 164, 178),
    2: (111, 208, 140),
    3: (74, 168, 255),
    4: (176, 124, 255),
    5: (255, 204, 77),
    6: (255, 138, 61),
}


def rarity_of(raw: str) -> int:
    """'TIER_5' -> 5。TIER_0 是异常值（装置），当 1 星处理。"""
    try:
        return max(1, min(6, int(str(raw).split("_")[-1])))
    except (ValueError, IndexError):
        return 1


def profession_cn(prof: str, sub: str = "", sub_names: dict[str, str] | None = None) -> str:
    """职业显示名。填了子职业映射就用子职业，否则退回大职业。"""
    if sub_names and sub:
        name = sub_names.get(sub)
        if name:
            return name
    return PROFESSION_CN.get(prof, "未知")


def faction_of(nation: str | None, group: str | None) -> str:
    """阵营显示：优先组织（更具体），其次国籍。两个都有就并起来。"""
    parts = []
    if group and GROUP_CN.get(group):
        parts.append(GROUP_CN[group])
    if nation and NATION_CN.get(nation):
        parts.append(NATION_CN[nation])
    return " · ".join(parts) or "未知"
