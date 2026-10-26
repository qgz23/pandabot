# -*- coding: utf-8 -*-
"""
自定义回复词典（JSON 驱动）
================================================================
把「机器人说什么」从代码里挪出来，放进一个 JSON 文件：

    config/replies.json

文件里每一条就是一个键值对：
    键   = 触发词（多个写法用 | 分隔）
    值   = 回复列表，命中后随机挑一条

几条约定（写在文件的 _说明 里，改文件的人一眼能看到）：
    - 以 _ 开头的键是注释/保留项，不会被当成触发词
    - 键写 "@" 表示「只@了机器人、没说话」时用的回复
    - 回复里可以写 {nick}，会替换成发消息人的群昵称
    - 支持两种命中：整句完全一致，或者消息以触发词开头
      （所以配了「你好」，群友发「你好呀」也能中；但配了「hi」，
        不会在 "this is hi" 这种句子里胡乱命中）
    - 只有一个字的触发词不做前缀匹配，否则「我」会吃掉大半句子

**不依赖 nonebot**：文件读写、匹配、热重载都在这里，插件层只负责收发消息，
所以这些逻辑可以脱离 bot 直接测。

热重载不用定时器：每次匹配前 stat 一下 mtime，变了就重读。
一条消息一次 stat，比开个轮询任务简单，也没有「改完要等 3 秒」的延迟。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

# 项目根目录（pandabot/），相对路径一律相对它，跟启动时的工作目录无关
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FILE = PROJECT_ROOT / "config" / "replies.json"

AT_KEY = "@"                # 只@机器人没说话
META_PREFIX = "_"           # 以下划线开头的是注释，不当触发词
MIN_PREFIX_LEN = 2          # 前缀匹配的最小触发词长度


DEFAULT_DATA: dict = {
    "_说明": [
        "键 = 触发词，多个写法用 | 分隔；值 = 回复列表，命中后随机挑一条",
        "以 _ 开头的键是注释，不会被当成触发词",
        "键写 @ 表示：只@了机器人、没说话的时候回这句",
        "回复里可以写 {nick}，会替换成发消息人的昵称",
        "命中规则：整句完全一样，或者消息以触发词开头（所以配「你好」能接住「你好呀」）",
        "改完存盘就生效（下一条消息用新内容），不用重启；也可以发 /重载回复 强制重读",
    ],
    "hello|你好|hi|在吗": [
        "你好呀{nick}！有什么我可以帮到你的吗？",
        "找我有事吗？",
        "在的在的，你说～",
    ],
    "@": [
        "叫我干嘛～",
        "在呢，直接说事就行",
        "{nick} 你艾特我但是没说话诶",
    ],
    "早安|早上好": ["早上好～今天也要元气满满"],
    "晚安|睡了": ["晚安，做个好梦"],
    "谢谢|感谢|谢了": ["不客气～", "举手之劳"],
}


def resolve_path(raw: str | None) -> Path:
    """配置里给的文件路径 → 绝对路径。空值用默认位置。"""
    if not raw:
        return DEFAULT_FILE
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def normalize(text: str) -> str:
    """去掉首尾和多余空白，英文转小写，方便比较。"""
    return " ".join((text or "").split()).casefold()


def split_keys(key: str) -> list[str]:
    """一个键可以写多个触发词：你好|hi|hello"""
    return [k.strip() for k in str(key).split("|") if k.strip()]


class ReplyBook:
    def __init__(self, path: str | Path | None = None, path_provider=None) -> None:
        """
        path           固定文件路径
        path_provider  可调用对象，返回文件路径（接配置热改）
        """
        self._fixed = Path(path) if path else None
        self._provider = path_provider
        self._entries: list[tuple[list[str], list[str]]] = []
        self._at_replies: list[str] = []
        self._mtime: float = -1.0
        self.last_error: str = ""

    # ---------------- 路径 ----------------
    def set_path_provider(self, fn) -> None:
        self._provider = fn

    @property
    def path(self) -> Path:
        raw = None
        if self._provider is not None:
            try:
                raw = self._provider()
            except Exception:
                raw = None
        return self._fixed or resolve_path(raw)

    # ---------------- 读写 ----------------
    def write_default(self) -> bool:
        """文件不存在时写一份带说明的默认模板。返回是否写了。"""
        path = self.path
        if path.exists():
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(DEFAULT_DATA, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
            return True
        except OSError:
            return False

    def load(self, *, force: bool = False) -> tuple[bool, str]:
        """读取词典。返回 (是否成功, 给人看的说明)。

        解析失败时**保留旧词典**——文件写坏了不该让机器人失声，
        用户改回去存盘，下一条消息自动恢复。
        """
        path = self.path
        if not path.exists():
            self.write_default()

        try:
            mtime = path.stat().st_mtime
        except OSError as e:
            self.last_error = f"读不到文件（{e.__class__.__name__}）"
            return False, self.last_error

        if not force and mtime == self._mtime and self._entries:
            return True, ""

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            self.last_error = f"JSON 格式有错：第 {e.lineno} 行第 {e.colno} 列，{e.msg}"
            return False, self.last_error
        except OSError as e:
            self.last_error = f"读不到文件（{e.__class__.__name__}）"
            return False, self.last_error

        if not isinstance(raw, dict):
            self.last_error = "顶层必须是 { } 这样的对象，不能是数组或单条字符串"
            return False, self.last_error

        entries: list[tuple[list[str], list[str]]] = []
        at_replies: list[str] = []
        skipped: list[str] = []

        for key, value in raw.items():
            name = str(key)
            if name.startswith(META_PREFIX):
                continue        # 注释项

            if isinstance(value, str):
                replies = [value]
            elif isinstance(value, list):
                replies = [str(v) for v in value if str(v).strip()]
            else:
                skipped.append(name)
                continue

            if not replies:
                skipped.append(name)
                continue

            if name.strip() == AT_KEY:
                at_replies = replies
                continue

            keys = [normalize(k) for k in split_keys(name)]
            keys = [k for k in keys if k]
            if not keys:
                skipped.append(name)
                continue
            entries.append((keys, replies))

        self._entries = entries
        self._at_replies = at_replies
        self._mtime = mtime
        self.last_error = ""

        msg = f"已加载 {len(entries)} 组触发词 / {self.reply_count()} 条回复"
        if skipped:
            msg += f"（跳过 {len(skipped)} 项：{'、'.join(skipped[:5])}）"
        return True, msg

    # ---------------- 查询 ----------------
    def reply_count(self) -> int:
        return sum(len(r) for _, r in self._entries) + len(self._at_replies)

    def ensure_fresh(self) -> bool:
        """文件改过就重读。返回是否发生了变化。"""
        try:
            if self.path.stat().st_mtime == self._mtime:
                return False
        except OSError:
            return False
        ok, _ = self.load(force=True)
        return ok

    def match(self, text: str, *, at_only: bool = False) -> str | None:
        """按消息内容挑一条回复。没命中返回 None。

        at_only=True 表示「只@了机器人、没说话」，走 @ 那一组。
        """
        if not self._entries and not self._at_replies:
            self.load()

        if at_only:
            return random.choice(self._at_replies) if self._at_replies else None

        msg = normalize(text)
        if not msg:
            return None

        # 「最具体的那个赢」：先找完全相等的，再找最长的前缀。
        # 不能靠在加载阶段按组排序——一组的多个别名长短不一，
        # 按「组里最长的词」排会让 "hi|hello|你好" 这组靠 "hello" 抢到前排，
        # 结果「你好呀」被这组的「你好」抢走。要比的是**命中的那个词**。
        best_exact: tuple[str, list[str]] | None = None
        best_prefix: tuple[str, list[str]] | None = None

        for keys, replies in self._entries:
            for key in keys:
                if msg == key:
                    if best_exact is None or len(key) > len(best_exact[0]):
                        best_exact = (key, replies)
                elif len(key) >= MIN_PREFIX_LEN and msg.startswith(key):
                    if best_prefix is None or len(key) > len(best_prefix[0]):
                        best_prefix = (key, replies)

        chosen = best_exact or best_prefix
        return random.choice(chosen[1]) if chosen else None

    def render(self, reply: str, *, nick: str = "") -> str:
        """把 {nick} 换成昵称。没提供就退化成「你」。"""
        return str(reply).replace("{nick}", nick or "你")


# 全局单例：插件层直接用
book = ReplyBook()
