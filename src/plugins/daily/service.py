# -*- coding: utf-8 -*-
"""
每日娱乐玩法：抽签逻辑与文案池
================================================================
**不 import 任何 nonebot 代码**，可以脱离 bot 单独跑测试。

五种玩法共用同一个结果结构 DailyResult，所以渲染层只要一套模板：
    今日运势 / 今日猪猪 / 今日超能力 / 今日 Doro 结局 / 哈基米

新增一个玩法只需要三步：
    1. 在这里加一个数据池常量
    2. 写一个 draw_xxx() 返回 DailyResult
    3. 在 __init__.py 里注册命令
渲染、降级文本、确定性保证全部自动继承。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.utils.daily import make_seed, pick, pick_distinct, pick_int, pick_weighted


@dataclass(frozen=True)
class Field:
    """卡片上的一行「标签 : 值」。color 非空时会在标签前画一个色点。"""

    label: str
    value: str
    color: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class DailyResult:
    kind: str                                   # fortune / pig / power / doro / hakimi
    title: str                                  # 卡片左上角标题
    date_str: str
    headline: str                                # 中间大字
    sub: str = ""                                # 大字下方说明
    fields: tuple[Field, ...] = ()
    line: str = ""                               # 底部一句话
    footer: str = "熊猫娘bot · 每天零点刷新"


# ====================================================================
# 玩法一：今日运势
# ====================================================================

# (等级, 权重, 一句话解释) —— 权重故意做成「吉多凶少」，抽签图个开心
FORTUNE_RANKS = (
    ("大吉", 7, "今天你是天选之人，想做的事放手去做。"),
    ("吉", 20, "顺风顺水，适合把拖着的事一次清完。"),
    ("中吉", 22, "稳中有进，别贪心就很好。"),
    ("小吉", 24, "小小的好事会发生，留心身边的人。"),
    ("末吉", 15, "开头有点钝，尾巴会慢慢翘起来。"),
    ("凶", 9, "低调一点，少说话，多喝水。"),
    ("大凶", 3, "今天诸事不宜，建议原地躺平保平安。"),
)

# (颜色名, RGB) —— RGB 用来在卡片上真的画一个色块
LUCKY_COLORS = (
    ("樱花粉", (255, 158, 195)),
    ("雾霾蓝", (126, 152, 189)),
    ("奶油白", (250, 244, 227)),
    ("抹茶绿", (150, 196, 141)),
    ("焦糖棕", (188, 138, 101)),
    ("葡萄紫", (154, 130, 200)),
    ("番茄红", (232, 106, 92)),
    ("柠檬黄", (247, 214, 96)),
    ("薄荷青", (128, 205, 196)),
    ("石墨灰", (108, 112, 122)),
)

LUCKY_ITEMS = (
    "耳机", "便利贴", "一颗糖", "旧外套", "冰美式", "透明雨伞",
    "地铁最后一节车厢", "洗干净的白袜子", "没读完的那本书", "窗台上的绿植",
    "口袋里放了三天的硬币", "刚好赶上的那个绿灯",
)

GOOD_DEEDS = (
    "摸鱼", "早睡", "吃夜宵", "听老歌", "整理房间", "给人点赞",
    "翻旧照片", "散步", "把想说的话说出口", "重看一部电影",
    "给自己买杯奶茶", "认真发一会呆", "把欠的道歉说了",
)

BAD_DEEDS = (
    "熬夜", "嘴硬", "冲动消费", "已读不回", "洗头", "加班",
    "翻旧账", "轻易许诺", "空着肚子喝咖啡", "把心里话憋着",
    "深夜做决定", "和键盘较劲", "在群里说「我睡了」",
)

# 底部锦囊：不按等级分，谁抽到哪句都是随机的
FORTUNE_TIPS = (
    "机会不会敲门，但今天有扇窗开着。",
    "不必赢过谁，今天只要比昨天多一点。",
    "把要说的话说出口，别放在草稿箱里。",
    "你今天的样子，已经比你想的好看多了。",
    "有人正惦记着你，只是还没找到借口开口。",
    "慢一点也没关系，方向对就行。",
    "今天适合原谅一个人，包括你自己。",
    "别小看一顿好饭的治愈力。",
    "困了就睡，明天的事明天再说。",
    "你不是不行，只是还没到时间。",
    "运气是留给愿意出门的人的。",
    "把「算了」换成「试试看」。",
    "今天不做决定也没关系，明天再想。",
    "被人喜欢不需要理由，但也别太用力。",
    "天气好的时候，记得抬头。",
    "你已经很棒了，不用每次都说服自己。",
)


def draw_fortune(*, date_str: str, user_id: str, group_id: str = "") -> DailyResult:
    seed = make_seed(date_str, user_id, "fortune", group_id)

    rank, _, note = pick_weighted([(item, item[1]) for item in FORTUNE_RANKS], seed + "|rank")
    color_name, color_rgb = pick(LUCKY_COLORS, seed + "|color")
    number = pick_int(seed + "|num", 1, 99)
    item = pick(LUCKY_ITEMS, seed + "|item")

    good = pick(GOOD_DEEDS, seed + "|good")
    # 宜和忌不能撞（两个池子不重叠，但保留这道防线，以后改文案也不会出错）
    bad = pick_distinct(BAD_DEEDS, seed + "|bad", exclude=[good])

    return DailyResult(
        kind="fortune",
        title="今日运势",
        date_str=date_str,
        headline=rank,
        sub=note,
        fields=(
            Field("幸运色", color_name, color_rgb),
            Field("幸运数字", str(number)),
            Field("幸运物", item),
            Field("宜", good),
            Field("忌", bad),
        ),
        line=pick(FORTUNE_TIPS, seed + "|tip"),
    )


# ====================================================================
# 玩法二：今日猪猪
# ====================================================================

# (称号, 调侃)
PIG_TITLES = (
    ("头号干饭猪", "今日食量惊人，建议自带饭桶"),
    ("瞌睡小香猪", "随时待机入睡，梦话频出"),
    ("天选摸鱼猪", "工作在面前稳如泰山，一动不动"),
    ("人间富贵猪", "今天的你，看起来很贵"),
    ("熬夜黑眼圈猪", "凌晨三点还在线，眼圈已成国宝"),
    ("话痨小花猪", "一个人能聊满一整屏"),
    ("社恐奶香猪", "想说话又不敢，但很可爱"),
    ("倔强表情猪", "嘴上说着不，身体很诚实"),
    ("隐形富婆猪", "不出声，但红包从不落下"),
    ("气氛担当猪", "它一说话，群里就活了"),
)

PIG_REASONS = (
    "据不完全统计，它今天已经摸了三次鱼。",
    "本群今日含猪量检测：它浓度最高。",
    "它今天的心态，非常猪。",
    "系统随机决定，它就是今天的猪。",
    "它昨晚肯定睡得比谁都香。",
    "没有理由，就是想叫它猪。",
    "它今天的发言，散发着猪猪的气息。",
)


def draw_pig(
    *,
    date_str: str,
    user_id: str,
    group_id: str,
    members: list[dict],
) -> DailyResult:
    """抽今日猪猪：候选是本群说过话的人，**包含自己**。

    为什么包含自己：只抽别人会出现「群里只有我一个人说话，那就永远抽不到」
    的死角。允许抽到自己反而更好笑，也不需要兜底二次元角色。
    """
    seed = make_seed(date_str, user_id, "pig", group_id)

    # 稳定排序：数据库返回顺序会随 last_seen 变化，不排序会导致结果漂移
    pool = sorted(
        (
            {"openid": str(m.get("openid") or ""), "nickname": str(m.get("nickname") or "").strip()}
            for m in members
            if str(m.get("nickname") or "").strip()
        ),
        key=lambda m: m["openid"],
    )

    if pool:
        chosen = pick(pool, seed + "|who")
        name = chosen["nickname"]
        solo = len(pool) <= 1
    else:
        # 数据库里一条记录都没有（新群/私聊）：就把抽签对象定成自己
        name = "你自己"
        solo = True

    title, tease = pick(PIG_TITLES, seed + "|title")
    reason = pick(PIG_REASONS, seed + "|reason")

    extra = "群里说话的人还太少，先委屈你了" if solo and pool else ""

    return DailyResult(
        kind="pig",
        title="今日猪猪",
        date_str=date_str,
        headline=name,
        sub=tease,
        fields=(
            Field("今日封号", title),
            Field("当选理由", reason),
            Field("候选人数", f"{len(pool)} 人" if pool else "0 人（仅自己）"),
        ),
        line=extra or "今日份猪猪已送达，请勿投喂过量。",
        footer="熊猫娘bot · 每天零点重新评选",
    )


# ====================================================================
# 玩法三：今日超能力
# ====================================================================

# 权重控制稀有度比例：SSR 少得让人有惊喜感，但也不能抽不到
RARITY_WEIGHTS = (("N", 38), ("R", 30), ("SR", 24), ("SSR", 8))

# (名称, 说明, 稀有度)
POWERS = (
    ("时间暂停三秒", "可以让世界静止三秒，但你也会跟着静止。", "SSR"),
    ("完美睡眠", "一沾枕头就睡着，醒来神清气爽，永不失眠。", "SSR"),
    ("记忆快照", "可以把任意一分钟存成一张照片，随时回看。", "SSR"),
    ("无理由好运", "每天有一次莫名其妙的好运，但不会告诉你是哪次。", "SSR"),
    ("蚊子绝缘", "全世界的蚊子对你彻底失去兴趣。", "SR"),
    ("猫语六级", "能听懂猫在说什么，内容多半是「喂我」。", "SR"),
    ("记忆删除键", "可以删掉一段不想要的记忆，但无法指定是哪一段。", "SR"),
    ("自动纠错", "你打错的字，会被对方自动理解成正确的意思。", "SR"),
    ("影子替你排队", "可以让影子替你做一件无聊的事，它会记得。", "SR"),
    ("一秒入戏", "看任何剧都能立刻入戏，也能立刻出戏。", "SR"),
    ("无限续杯", "任何饮料喝到底会自动满上，但温度随机。", "R"),
    ("免排队体质", "你排的那一队永远是最快的。", "R"),
    ("电量守护神", "手机永远剩 1% 却不会关机。", "R"),
    ("静音结界", "周围三米自动降噪，包括你想听的那句话。", "R"),
    ("体重不涨", "随便吃，体重永远不变，但裤子会变小。", "R"),
    ("电梯直达", "等电梯永远是第一个到。", "N"),
    ("一句话冷场", "你说的每句话都能让现场安静三秒。", "N"),
    ("反向堵车", "你走哪条路堵哪条；你提前十分钟，世界晚十分钟。", "N"),
    ("天气预报嘴", "你说会下雨天就下雨。你说不准。", "N"),
    ("别人家的小孩", "在任何一个家长眼里，你都是别人家的小孩。", "N"),
)

RARITY_NOTE = {
    "N": "平平无奇，但胜在稳定",
    "R": "有点用，关键时刻能救命",
    "SR": "这个能力，值得在群里炫耀",
    "SSR": "抽到这个，今天可以横着走",
}


def draw_power(*, date_str: str, user_id: str, group_id: str = "") -> DailyResult:
    seed = make_seed(date_str, user_id, "power", group_id)

    rarity = pick_weighted(RARITY_WEIGHTS, seed + "|rarity")
    bucket = [p for p in POWERS if p[2] == rarity] or list(POWERS)
    name, desc, rarity = pick(bucket, seed + "|power")

    return DailyResult(
        kind="power",
        title="今日超能力",
        date_str=date_str,
        headline=name,
        sub=desc,
        fields=(
            Field("稀有度", rarity),
            Field("评价", RARITY_NOTE.get(rarity, "无法评价")),
            Field("有效期", "今天，仅今天"),
        ),
        line="能力明天零点重置，请及时使用。",
    )


# ====================================================================
# 玩法四：今日 Doro 结局
# ====================================================================

# (结局名, 描述, 类型)
DORO_ENDINGS = (
    ("橘子传递结局", "下班路上你遇到一只 doro。它不说话，只是把最后一颗橘子塞进你手里，然后蹦蹦跳跳地走了。", "温暖"),
    ("一起发呆结局", "你和 doro 在马路牙子上坐了两个小时，什么都没干，也没觉得浪费。", "治愈"),
    ("尾随回家结局", "doro 一路跟着你回家，进门后径直躺上你的床，理直气壮。", "离谱"),
    ("便利店分赃结局", "你和 doro 一人一半分了一根火腿肠，它吃了大的那半。", "平等"),
    ("突然跳舞结局", "你在等红灯，doro 在旁边跳了一支没有音乐的舞，路人都在看你。", "尴尬"),
    ("雨中相遇结局", "下雨了你没带伞，doro 递过来一片叶子。你居然觉得够用。", "浪漫"),
    ("无声告别结局", "doro 看了你一眼，转身走了。你不知道它叫什么，但记住了那只橘子。", "感伤"),
    ("电梯共乘结局", "你和 doro 在电梯里沉默了十八层，门开时它先出去，还回头点了下头。", "微妙"),
    ("抢座结局", "地铁上只剩一个座位，你和 doro 对视三秒，它赢了。", "认输"),
    ("加班陪伴结局", "你加班到十一点，doro 一直在桌角坐着，什么都没做，但你没那么难熬了。", "陪伴"),
    ("冷战结局", "你踩了 doro 的脚，它转身就走。第二天它像什么都没发生过。", "和解"),
    ("收留结局", "你在楼道里发现一只 doro，从此你家多了一个不交房租的室友。", "同居"),
)


def draw_doro(*, date_str: str, user_id: str, group_id: str = "") -> DailyResult:
    seed = make_seed(date_str, user_id, "doro", group_id)

    name, desc, tag = pick(DORO_ENDINGS, seed + "|ending")
    code = f"DORO-{pick_int(seed + '|code', 1, 99):02d}"
    minutes = pick_int(seed + "|min", 3, 240)

    return DailyResult(
        kind="doro",
        title="今日 Doro 结局",
        date_str=date_str,
        headline=name,
        sub=desc,
        fields=(
            Field("结局编号", code),
            Field("结局类型", tag),
            Field("陪伴时长", f"{minutes} 分钟"),
        ),
        line="明天会有一条新的支线等你解锁。",
        footer="熊猫娘bot · 每天零点解锁新结局",
    )


# ====================================================================
# 玩法五：哈基米
# ====================================================================

# (类型, 特征)
HAKIMI_TYPES = (
    ("橘色哈基米", "饭点一到就出现在你脚边，从不失约"),
    ("奶牛哈基米", "情绪稳定得像假的，直到看见纸箱"),
    ("三花哈基米", "今天的心情取决于风向"),
    ("白毛哈基米", "看起来很高冷，其实只是在发呆"),
    ("狸花哈基米", "本小区战斗力天花板，但很讲武德"),
    ("布偶哈基米", "被抱起来就自动软掉，毫无原则"),
    ("黑猫哈基米", "每天早上六点准时踩你的脸"),
    ("折耳哈基米", "什么都不做就很可爱，属于天赋型选手"),
)

HAKIMI_ORDERS = (
    "去阳台晒十分钟太阳，什么都不许想。",
    "现在立刻喝一杯水，喝完再说话。",
    "给一个很久没联系的人发条消息。",
    "什么都不做，允许自己浪费一点时间。",
    "去楼下走一圈，不许戴耳机。",
    "把桌上那杯冷掉的咖啡倒掉。",
    "早点睡，明天真的会不一样。",
    "今天少说一句反驳的话，试试看。",
    "摸摸身边的毛茸茸（有的话）。",
    "吃点热的，别对付。",
)

HAKIMI_LINES = (
    "哈基米不是一只猫，是一种生活态度。",
    "今天的你，值得被摸头。",
    "保持好奇，保持犯困。",
    "不必乖，但要开心。",
    "所有的好运都在你打哈欠的时候悄悄来了。",
    "躺着不是懒，是在省电。",
    "今天也要理直气壮地存在。",
)


def draw_hakimi(*, date_str: str, user_id: str, group_id: str = "") -> DailyResult:
    seed = make_seed(date_str, user_id, "hakimi", group_id)

    name, trait = pick(HAKIMI_TYPES, seed + "|type")
    index = pick_int(seed + "|index", 45, 100)

    return DailyResult(
        kind="hakimi",
        title="今日哈基米",
        date_str=date_str,
        headline=name,
        sub=trait,
        fields=(
            Field("哈基米指数", f"{index} / 100"),
            Field("今日指令", pick(HAKIMI_ORDERS, seed + "|order")),
        ),
        line=pick(HAKIMI_LINES, seed + "|line"),
    )


# ====================================================================
# 降级文本（图片渲染 / 发送失败时用）
# ====================================================================


def to_text(r: DailyResult) -> str:
    parts = [f"【{r.title}】{r.headline}"]
    if r.sub:
        parts.append(r.sub)
    for f in r.fields:
        parts.append(f"{f.label}：{f.value}")
    if r.line:
        parts.append(f"「{r.line}」")
    return "\n".join(parts)


__all__ = [
    "DailyResult",
    "Field",
    "draw_fortune",
    "draw_pig",
    "draw_power",
    "draw_doro",
    "draw_hakimi",
    "to_text",
]
