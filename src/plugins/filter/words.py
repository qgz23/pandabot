# -*- coding: utf-8 -*-
"""
敏感词词库：加载、归一化、匹配
================================================================
纯逻辑，不依赖 NoneBot —— 可以脱离 bot 单独跑测试。

词库放 ``config/banned_words.json``，改完存盘就生效（按 mtime 自动重载），
不用重启，也不用碰任何 .py。

匹配的核心是**先归一化再查**：
    「敏·感」「敏 感」「ＭＩＮ感」归一化后都是「敏感」
    这一步能挡掉「插符号 / 全角 / 空格」这三种最常见的绕词手法。

它挡不住的是纯谐音（「沙比」对「傻逼」）和拆字（「亻本」），
那类只能往词库里加变体，没有任何通用算法能自动解。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

# 归一化时保留的字符：0-9、a-z、中日韩汉字。其余（标点、空格、emoji、符号）全删。
_KEEP = re.compile(r"[^0-9a-z\u4e00-\u9fff]")

# 白名单挖出来的洞。不能用空字符串替换 —— 删掉之后两侧会拼一起，
# 可能诞生一个原本不存在的新词。
_HOLE = "\x00"


class Hit(NamedTuple):
    """命中的词和它所属的分类。"""

    word: str
    category: str


def normalize(text: str) -> str:
    """全角转半角、转小写、只留中文/字母/数字。

    「Ａ-１」「敏·感」「敏 感」处理完都是 ``a1`` / ``敏感`` / ``敏感``。
    """
    if not text:
        return ""
    chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII（含全角标点）
            ch = chr(code - 0xFEE0)
        elif code == 0x3000:  # 全角空格
            ch = " "
        chars.append(ch.lower())
    return _KEEP.sub("", "".join(chars))


# 文件不存在时自动生成的模板。写完就能改。
DEFAULT_DATA = {
    "_说明": [
        "categories：分类名 → 敏感词列表，命中任意一个就按 actions 处理",
        "actions.recall：撤回违规消息（需要机器人是群管理员）",
        "actions.notify：撤回后在群里说一句；notify_cooldown_seconds 是同群提示冷却，防刷屏",
        "whitelist：豁免词。匹配前先把它们从消息里挖掉，防止「颜色」被「色」误伤",
        "groups：留空 = 所有群生效；填群 openid 则只在这些群生效",
        "以 _ 开头的键是注释，不会被当成分类",
    ],
    "enabled": True,
    "actions": {
        "recall": True,
        "notify": True,
        "notify_text": "已处理一条违规消息",
        "notify_cooldown_seconds": 30,
    },
    "groups": [],
    "whitelist": ["颜色", "色彩", "色谱", "出色", "特色"],
    "categories": {
        "广告": ["加微信", "加v", "私聊我", "代练", "刷钻", "低价出", "扫码进群", "免费领取"],
        "骂人": ["傻逼", "沙比", "草泥马", "nmsl", "cnm"],
        "其他": [],
    },
}


class WordBook:
    """词库本体。设计上允许直接替换 path，方便单测。"""

    def __init__(self, path: str | Path = "config/banned_words.json"):
        self.path = Path(path)
        self.enabled = True
        self.recall = True
        self.notify = True
        self.notify_text = "已处理一条违规消息"
        self.cooldown = 30.0
        self.groups: set[str] = set()
        self.whitelist: list[str] = []
        self.entries: list[tuple[str, str]] = []
        self.last_error = ""
        self._mtime = 0.0

    # ---------------- 文件 ----------------
    def write_default(self) -> bool:
        """词库文件不存在时写一份模板，返回是否真的写了。"""
        if self.path.exists():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(DEFAULT_DATA, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True

    def reload_if_changed(self) -> None:
        """按 mtime 判断要不要重读。每条消息都会调，必须够快。"""
        try:
            if self.path.stat().st_mtime != self._mtime:
                self.load()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def load(self, force: bool = False) -> tuple[bool, str]:
        """读词库。失败时保留旧词库继续用，不让过滤功能突然失声。"""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self.last_error = f"词库文件不存在：{self.path}"
            return False, self.last_error

        if not force and mtime == self._mtime:
            return True, f"{len(self.entries)} 个词，没有变化"

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("顶层必须是 JSON 对象 { }")
        except Exception as e:
            self.last_error = f"词库解析失败：{e!r}"
            return False, self.last_error

        actions = data.get("actions")
        if not isinstance(actions, dict):
            actions = {}

        entries: list[tuple[str, str]] = []
        categories = data.get("categories")
        if isinstance(categories, dict):
            for name, words in categories.items():
                if str(name).startswith("_") or not isinstance(words, list):
                    continue
                for word in words:
                    norm = normalize(str(word))
                    if norm:
                        entries.append((norm, str(name)))

        # 长的排前面：命中「加微信」比命中「微信」更能说明问题
        entries.sort(key=lambda item: (-len(item[0]), item[0]))

        whitelist = [
            normalize(str(w)) for w in (data.get("whitelist") or []) if str(w).strip()
        ]
        whitelist = [w for w in whitelist if w]
        # 长的先挖：否则「颜色」会被更短的词先挖掉一部分
        whitelist.sort(key=len, reverse=True)

        raw_groups = data.get("groups")
        groups = {
            str(g) for g in (raw_groups if isinstance(raw_groups, list) else []) if g
        }

        # 全部校验通过，才一次性替换，避免读到一半失败留下半套配置
        self.enabled = bool(data.get("enabled", True))
        self.recall = bool(actions.get("recall", True))
        self.notify = bool(actions.get("notify", True))
        self.notify_text = str(actions.get("notify_text") or "已处理一条违规消息")
        try:
            self.cooldown = max(0.0, float(actions.get("notify_cooldown_seconds", 30)))
        except (TypeError, ValueError):
            self.cooldown = 30.0
        self.groups = groups
        self.whitelist = whitelist
        self.entries = entries
        self._mtime = mtime
        self.last_error = ""

        return True, self.summary()

    # ---------------- 查询 ----------------
    def summary(self) -> str:
        cats = {category for _, category in self.entries}
        return (
            f"{len(cats)} 个分类 / {len(self.entries)} 个词，"
            f"白名单 {len(self.whitelist)} 个"
        )

    def allow_group(self, group_openid: str) -> bool:
        """群白名单为空表示全群生效。"""
        return not self.groups or group_openid in self.groups

    def match(self, text: str) -> Hit | None:
        """命中则返回 ``Hit``，否则 None。"""
        if not self.enabled:
            return None

        norm = normalize(text)
        if not norm:
            return None

        # 先挖白名单，再查敏感词
        for hole in self.whitelist:
            if hole in norm:
                norm = norm.replace(hole, _HOLE)

        for word, category in self.entries:
            if word in norm:
                return Hit(word, category)
        return None


__all__ = ["Hit", "WordBook", "normalize", "DEFAULT_DATA"]
