# -*- coding: utf-8 -*-
"""
群友老婆：从本群成员里抽一个人当今天的「老婆」
================================================================
和 waifu 插件（抽群友上传的角色）不是一回事：
    waifu   池子是**图片**，抽的是二次元角色，出图
    couple  池子是**活人**，抽的是本群发过言的群友，出文字

**不依赖 nonebot / PIL**，纯计算，可脱离 bot 直接测试。

候选池来源：userstore 的 users 表，按 session_id 前缀 `group_{群}_%` 捞出本群有发言记录的人。
所以「抽到的人」天然等于「最近说过话的人」——潜水的抽不到，
这算是个副产品激励，也让池子不会无限膨胀。

确定性抽签（复用 utils/daily.py）：
    同一天 + 同一个群 + 同一个人 → 永远是同一位群友，第二天自动换。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.utils.daily import h, make_seed, pick, pick_int

# 关系称号。想加就往后加，不用改逻辑。
TITLES = (
    "命中注定",
    "天作之合",
    "冤家路窄",
    "隔壁邻居",
    "青梅竹马",
    "临时搭伙",
    "相爱相杀",
    "塑料夫妻",
    "一生之敌",
    "孽缘",
    "官方认证",
    "迷惑搭配",
    "酒肉朋友",
    "同病相怜",
    "聊得来的陌生人",
    "话痨与哑巴",
)

COMMENTS = (
    "别问为什么，问就是缘分到了。",
    "你们俩站一块儿，画风意外地统一。",
    "建议今天多聊两句，明天的老婆就换人了。",
    "这段关系的稳定性，大概和食堂的菜一样。",
    "抽签结果不代表本人意愿，请勿当真。",
    "看起来不太配，但谁知道呢。",
    "本群官方祝福，虽然没人征求过你们意见。",
    "今天请对这个人好一点。",
    "匹配度仅供参考，真正的生活还得自己过。",
    "这一刻起，你们在某种意义上是一家人了。",
    "别尴尬，明天就轮到别人了。",
    "据说这种组合最容易一起搞事情。",
    "缘分这东西，抽到谁算谁。",
    "如果你们本来就有仇，那今天尤其尴尬。",
    "建议合照一张，明天就再也抽不到同一个人了。",
    "本群媒婆业务纯属娱乐，不承担任何后果。",
    "抽到不代表什么，但被抽到的人可能会来找你。",
    "别急着解释，解释就是掩饰。",
    "这一对，说是天意也行，说是算法也行。",
    "今天的运气全花在这上面了。",
    "你们之间故事还没开始，但已经有人开始笑了。",
    "祝你们今天相处愉快，明天各奔东西。",
    "这个组合有点东西，具体什么东西不好说。",
    "本群第 N 次乱点鸳鸯谱，成果依旧不明。",
)

# 自己抽到自己的概率是 0，所以除自己之外至少还要有 1 个人
MIN_CANDIDATES = 1


@dataclass(frozen=True)
class CoupleResult:
    date_str: str
    name: str          # 群昵称
    uid: str           # 对方的 session_id，用于回查资料
    cn: str = ""       # 对方的 CN（如果录过 /信息）
    title: str = ""
    percent: int = 0
    comment: str = ""
    pool_size: int = 0  # 候选人数（已排除自己）
    salt: str = "wife"


def draw(
    *,
    date_str: str,
    group_openid: str,
    user_uid: str,
    members: list[dict],
    salt: str = "wife",
) -> CoupleResult | None:
    """抽一位群友。池子不够（除自己外没人）返回 None。

    members: [{"session_id":..., "openid":..., "nickname":...}, ...]
    CN 不在这里查：抽中之后再补查那一个人，比给整池子查一遍资料便宜得多。
    """
    candidates = [m for m in members if str(m.get("session_id") or "") != user_uid]

    # **必须按 session_id 排序**：上游是 ORDER BY last_seen DESC，
    # 群里每有人说句话顺序就变，同一天能抽出不同的人来。
    # 候选集合没变、顺序变了，结果就不能变——排序是把顺序这件事彻底钉死。
    candidates.sort(key=lambda m: str(m.get("session_id") or ""))
    if len(candidates) < MIN_CANDIDATES:
        return None

    seed = make_seed(date_str, user_uid, f"couple-{salt}", group_openid)
    target = candidates[h(seed + "|who") % len(candidates)]
    uid = str(target.get("session_id") or "")

    return CoupleResult(
        date_str=date_str,
        name=str(target.get("nickname") or "").strip() or "某位群友",
        uid=uid,
        title=pick(TITLES, seed + "|title"),
        percent=pick_int(seed + "|pct", 55, 99),
        comment=pick(COMMENTS, seed + "|comment"),
        pool_size=len(candidates),
        salt=salt,
    )


def to_text(r: CoupleResult) -> str:
    """纯文字版（发图失败时的兜底也是这个）。"""
    who = f"{r.name}（CN {r.cn}）" if r.cn else r.name
    head = "今日群友老婆" if r.salt == "wife" else "今日群友老公"
    return (
        f"{head}：{who}\n"
        f"关系：{r.title} · 匹配度 {r.percent}%\n"
        f"「{r.comment}」\n"
        f"—— {r.date_str} · 本群候选 {r.pool_size} 人 · 明天零点换人"
    )
