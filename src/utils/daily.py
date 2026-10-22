# -*- coding: utf-8 -*-
"""
每日抽签引擎（被多个娱乐玩法共用）
================================================================
**不依赖 nonebot / PIL**，纯计算，可以脱离一切环境直接测试。

核心思想：**确定性随机**。
    给定同一个 seed 字符串，永远算出同一个结果。
    所以只要 seed 里带上「日期 + 群 + 用户 + 玩法名」，
    就自动实现了「每天每人每个玩法抽一次，当天重复发不变，第二天自动换」。

为什么不用数据库记录"今天抽过什么"：
    - 零状态：不用建表、不用跨天清理、重启不丢
    - 天然幂等：并发发两次也不会冲突，也不会消耗两次运气
    - 成本为零：一次 sha256 比一次数据库往返快几个数量级

为什么不用内置 hash()：
    Python 3.3+ 对 str 的 hash 加了口令盐（PYTHONHASHSEED），
    **每次进程重启结果都不一样**。用它做抽签会出现
    "今天的老婆在重启 bot 之后变成了别人"。必须用 hashlib.sha256。
"""

from __future__ import annotations

import hashlib
from typing import Sequence, TypeVar

T = TypeVar("T")


def digest(seed: str) -> bytes:
    """sha256 摘要。结果稳定、跨进程一致、无随机盐。"""
    return hashlib.sha256(seed.encode("utf-8")).digest()


def h(seed: str) -> int:
    """把 seed 映射成 64 位正整数。取前 8 字节，够大且分布均匀。"""
    return int.from_bytes(digest(seed)[:8], "big")


def pick(seq: Sequence[T], seed: str) -> T:
    """从序列里确定性抽一个元素。"""
    if not seq:
        raise ValueError("pick() 收到空序列，无法抽取")
    return seq[h(seed) % len(seq)]


def pick_weighted(options: Sequence[tuple[T, int]], seed: str) -> T:
    """按权重确定性抽取。

    options: [(值, 权重), ...]，权重是相对值，不用凑成 100。
    典型用途：抽稀有度（SSR 少、N 多）或抽运势等级（大吉少、小吉多）。
    """
    if not options:
        raise ValueError("pick_weighted() 收到空序列")
    total = sum(w for _, w in options)
    if total <= 0:
        raise ValueError("pick_weighted() 的权重总和必须大于 0")
    roll = h(seed) % total
    acc = 0
    for value, weight in options:
        acc += weight
        if roll < acc:
            return value
    return options[-1][0]  # 理论上走不到，保险


def pick_int(seed: str, low: int, high: int) -> int:
    """在 [low, high] 闭区间内确定性取整数。"""
    if high < low:
        low, high = high, low
    return low + h(seed) % (high - low + 1)


def pick_distinct(seq: Sequence[T], seed: str, exclude: Sequence[T] = ()) -> T:
    """抽一个不在 exclude 里的元素。

    用于「宜」和「忌」不能是同一件事这种场景：
    抽到的若撞上了，就用带后缀的新 seed 重抽一次（最多几次，避免死循环）。
    """
    if not seq:
        raise ValueError("pick_distinct() 收到空序列")
    if len(exclude) >= len(seq):
        return seq[h(seed) % len(seq)]
    value = pick(seq, seed)
    for i in range(1, 8):
        if value not in exclude:
            return value
        value = pick(seq, f"{seed}#retry{i}")
    return value


def make_seed(
    date_str: str,
    user_id: str,
    kind: str,
    group_id: str = "",
) -> str:
    """拼装标准 seed。

    群维度是可选加进去的：像「今日猪猪」这种要抽本群的人，必须带群；
    像「今日运势」这种只看自己的，带上群也无副作用（人只会更少重叠）。
    """
    scope = group_id or "private"
    return f"{date_str}|{scope}|{user_id}|{kind}"
