# -*- coding: utf-8 -*-
"""
今日老婆：抽签逻辑（纯函数 + 确定性随机）
================================================================
**不 import 任何 nonebot 代码**，可脱离 bot 单独测试。

数据模型：
    一次上传 = 一条记录（角色名 + 一张图 + 一句台词 + 上传者）
    **同一个角色名可以有多条记录**（不同群友传同一角色的不同图）

所以抽签是**两层**的：
    第一层抽角色名（从去重后的名字里抽）
    第二层从该角色名下的所有图里再抽一张（连带它的台词和上传者）

为什么要分两层，而不是直接在所有记录里抽：
    直接抽的话，一个角色被传了 10 张、另一个只传了 1 张，
    前者会以 10 倍概率出现，等于"谁刷图谁霸榜"。先抽名字再抽图，
    每个角色机会均等，同角色多图只是"同一角色的不同立绘"。

确定性：
    同一天 + 同一个群 + 同一个人 → 角色和图片都不变，第二天自然换。
    零状态，不用建表记录抽过什么。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.utils.daily import h


@dataclass(frozen=True)
class WaifuResult:
    date_str: str
    name: str
    image: str = ""          # 图片文件名（由 roles.py 定位到实际路径）
    line: str = ""           # 角色出现时说的话（上传者填的）
    uploader: str = ""       # 上传者昵称
    role_id: str = ""
    pool_size: int = 0       # 本群有多少个不同角色
    variant_count: int = 0   # 抽中的这个角色有几张图
    salt: str = "wife"       # wife / husband


def draw(
    *,
    date_str: str,
    group_openid: str,
    querier_openid: str,
    roles: list,
    salt: str = "wife",
) -> WaifuResult | None:
    """从本群角色池里抽一个角色，再抽它的一张图。

    roles: roles.Role 列表（可能有同名多条）
    池子为空返回 None，让上层去提示"还没人上传"。

    两处都必须排序：名字和 id 都要稳定。
    上传、删除会改变 list 顺序，不排序的话同一个下标会指向不同角色，
    表现就是"明明同一天，结果却变了"。
    """
    if not roles:
        return None

    # 第一层：按角色名分组
    buckets: dict[str, list] = {}
    for r in roles:
        name = str(getattr(r, "name", "") or "").strip()
        if name:
            buckets.setdefault(name, []).append(r)
    if not buckets:
        return None

    base = f"{date_str}|{group_openid or 'private'}|{querier_openid}|{salt}"

    names = sorted(buckets)
    chosen_name = names[h(base + "|name") % len(names)]

    # 第二层：这个角色的图池里抽一张
    variants = sorted(buckets[chosen_name], key=lambda r: str(getattr(r, "id", "")))
    chosen = variants[h(base + "|variant") % len(variants)]

    return WaifuResult(
        date_str=date_str,
        name=chosen_name,
        image=str(getattr(chosen, "image", "") or ""),
        line=str(getattr(chosen, "line", "") or ""),
        uploader=str(getattr(chosen, "by_name", "") or ""),
        role_id=str(getattr(chosen, "id", "") or ""),
        pool_size=len(names),
        variant_count=len(variants),
        salt=salt,
    )


def to_text(r: WaifuResult) -> str:
    """文字部分：角色名 + 台词 + 上传者。

    这是**主输出**（图另发一条），不是降级兜底。
    三段之间空一行：QQ 里连在一起会挤成一坨，台词和署名分不清边界。
    """
    who = "今日老婆" if r.salt == "wife" else "今日老公"
    blocks = [f"{who}：{r.name}"]
    if r.line:
        blocks.append(f"「{r.line}」")
    if r.uploader:
        blocks.append(f"—— 由 {r.uploader} 上传")
    return "\n\n".join(blocks)
