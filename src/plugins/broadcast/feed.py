# -*- coding: utf-8 -*-
"""
定时播报配置（JSON 驱动）
================================================================
机器人隔一段时间在群里主动说一句话。说什么、多久说一次、轮播还是随机，
全部放在：**config/broadcast.json**

为什么单独一个文件，而不是塞进 settings.json：
    settings.json 是「功能开关」性质，改一次全局受影响；
    播报内容是**文案**，要频繁增删改，而且一写就是几十条句子。
    混在一起会互相干扰，还容易把开关改坏。
    跟 replies.json 的定位一致：文案归文案，开关归开关。

文件结构：
    {
      "enabled": true,
      "quiet_hours": [23, 8],
      "daily_limit": 24,
      "groups": [],
      "tasks": [
        {"name": "闲聊", "mode": "rotate", "every": 180, "messages": ["…", "…"]}
      ]
    }

**不依赖 nonebot**：解析、校验、挑句子、进度持久化都在这里。
插件层只管「什么时候发」和「怎么发」，所以这段逻辑能脱离 bot 直接测。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

# 项目根目录（pandabot/），相对路径一律相对它，跟启动时的工作目录无关
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FILE = PROJECT_ROOT / "config" / "broadcast.json"
DEFAULT_STATE = PROJECT_ROOT / "data" / "broadcast" / "state.json"

META_PREFIX = "_"            # 以下划线开头的是注释项
DEFAULT_EVERY = 180.0        # 没写 every 时默认 3 小时（分钟）
MAX_MESSAGES_WARN = 500      # 单个任务句子太多时提醒一下（防手滑粘贴一堆）

MODE_ROTATE = "rotate"       # 轮播：按顺序一条条来
MODE_RANDOM = "random"       # 随机：每条机会均等
VALID_MODES = (MODE_ROTATE, MODE_RANDOM)


DEFAULT_DATA: dict = {
    "_说明": [
        "这个文件控制机器人在群里主动说话。改完存盘，最多 30 秒自动生效，不用重启。",
        "enabled：总开关。false 就是全部停掉",
        "quiet_hours：[开始小时, 结束小时]，这段时间不发。写 [23, 8] 表示 23 点到次日 8 点，跨零点也支持",
        "daily_limit：每个群每天最多发几条，防止刷屏和触发平台频控",
        "groups：留空 [] = 往所有加过机器人的群发；填了就只发这几个群（写群 openid）",
        "tasks：可以写多个任务，各自独立计时",
        "  name     任务名，随便起，进度按这个名字记（别重名）",
        "  mode     rotate = 按顺序轮播；random = 随机挑",
        "  every    多久发一次，单位分钟。写 180 = 固定 3 小时；写 \"60-180\" = 在这个区间里随机",
        "  messages 句子列表。可以写 {date}、{time}，发送时替换成日期和时间",
        "  hours    可选，这个任务自己的时段，写法同 quiet_hours",
        "  enabled  可选，单独停掉某个任务写 false",
        "想看当前状态发 /播报；想现在就试一条发 /播报 立即",
    ],
    "enabled": True,
    "quiet_hours": [23, 8],
    "daily_limit": 24,
    "groups": [],
    "tasks": [
        {
            "name": "闲聊",
            "mode": "rotate",
            "every": 180,
            "messages": [
                "冒个泡，群里有活人吗",
                "今天也是适合打舞萌的一天",
                "熊猫娘在线，有事叫我",
                "刚刚路过，顺便问一句：有人在吗",
            ],
        },
        {
            "name": "随机唠叨",
            "mode": "random",
            "every": "240-480",
            "messages": [
                "记得喝水",
                "久坐了起来动一下",
                "今天签到签了吗",
                "别忘了给老婆投票（不是）",
            ],
        },
    ],
}


# ====================================================================
# 解析小工具
# ====================================================================
def resolve_path(raw: str | None, default: Path) -> Path:
    """配置里给的文件路径 → 绝对路径。空值用默认位置。"""
    if not raw:
        return default
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def parse_every(value, default: float = DEFAULT_EVERY) -> tuple[float, float]:
    """every 字段 → (最短分钟, 最长分钟)。

    支持三种写法：
        180            数字，固定 180 分钟
        "180"          字符串数字，同上
        "60-180"       区间，每次在 60~180 分钟之间随机
    """
    if isinstance(value, bool):
        return (default, default)
    if isinstance(value, (int, float)):
        v = float(value)
        return (v, v) if v > 0 else (default, default)

    raw = str(value or "").strip().lower()
    for unit in ("分钟", "min", "分", "m"):
        raw = raw.replace(unit, "")
    raw = raw.strip()

    if "-" in raw:
        lo, _, hi = raw.partition("-")
        try:
            a, b = float(lo), float(hi)
        except ValueError:
            return (default, default)
        a, b = min(a, b), max(a, b)
        return (a, b) if a > 0 and b > 0 else (default, default)

    try:
        v = float(raw)
    except ValueError:
        return (default, default)
    return (v, v) if v > 0 else (default, default)


def parse_hours(value) -> tuple[int, int] | None:
    """[23, 8] → (23, 8)。写错就当没配（不限时段）。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        start, end = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= start <= 24 and 0 <= end <= 24):
        return None
    return (start, end)


def in_window(hour: int, window: tuple[int, int] | None) -> bool:
    """当前小时是否落在 **这个窗口里面**。

    跨零点要特殊处理：[23, 8] 表示 23、0、1…7 点都在窗口内，
    这时候 start > end，判断条件要反过来用「或」。
    """
    if not window:
        return True
    start, end = window
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def may_send(
    hour: int,
    quiet: tuple[int, int] | None,
    active: tuple[int, int] | None = None,
) -> bool:
    """当前时间能不能发。

    两个窗口含义相反，别搞混：
        quiet   禁发时段（quiet_hours），落在里面就不发
        active  只在这个时段发（任务自己的 hours），落在外面就不发

    ⚠️ 这里踩过坑：一开始把 quiet_hours 直接当「允许时段」传给 in_window，
    结果配置写 [23, 8] 变成「只在半夜发」，白天一条都不发，而且不报错。
    """
    if quiet and in_window(hour, quiet):
        return False
    if active and not in_window(hour, active):
        return False
    return True


# ====================================================================
# 任务
# ====================================================================
@dataclass
class Task:
    name: str
    mode: str = MODE_ROTATE
    low: float = DEFAULT_EVERY          # 最短间隔（分钟）
    high: float = DEFAULT_EVERY         # 最长间隔（分钟）
    messages: list[str] = field(default_factory=list)
    hours: tuple[int, int] | None = None

    @property
    def mode_label(self) -> str:
        return "轮播" if self.mode == MODE_ROTATE else "随机"

    @property
    def every_label(self) -> str:
        if self.high <= self.low:
            return f"{self.low:g} 分钟"
        return f"{self.low:g}~{self.high:g} 分钟"

    def next_gap(self) -> float:
        """下一次间隔，单位秒。区间型就在这里摇一次。"""
        if self.high <= self.low:
            return self.low * 60.0
        return random.uniform(self.low, self.high) * 60.0


# ====================================================================
# 配置读取
# ====================================================================
class FeedBook:
    """broadcast.json 的读取、校验、挑句子。"""

    def __init__(self, path: str | Path | None = None, path_provider=None) -> None:
        self._fixed = Path(path) if path else None
        self._provider = path_provider
        self.tasks: list[Task] = []
        self.enabled: bool = True
        self.quiet_hours: tuple[int, int] | None = None
        self.daily_limit: int = 24
        self.group_filter: list[str] = []
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
        return self._fixed or resolve_path(raw, DEFAULT_FILE)

    # ---------------- 读写 ----------------
    def write_default(self) -> bool:
        """文件不存在时写一份带说明的模板。返回是否写了。"""
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
        """读取配置。返回 (是否成功, 给人看的说明)。

        坏文件**保留旧配置**——写坏 JSON 不该让播报突然变成每 30 秒狂发，
        也不该让它直接静默消失。改回去存盘就自动恢复。
        """
        path = self.path
        if not path.exists():
            self.write_default()

        try:
            mtime = path.stat().st_mtime
        except OSError as e:
            self.last_error = f"读不到文件（{e.__class__.__name__}）"
            return False, self.last_error

        if not force and mtime == self._mtime and (self.tasks or not self.enabled):
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
            self.last_error = "顶层必须是 { } 这样的对象，不能是数组"
            return False, self.last_error

        tasks: list[Task] = []
        problems: list[str] = []
        seen: dict[str, int] = {}

        for idx, item in enumerate(raw.get("tasks") or [], start=1):
            if not isinstance(item, dict):
                problems.append(f"第 {idx} 个任务不是 {{ }}")
                continue
            if item.get("enabled", True) is False:
                continue

            name = str(item.get("name") or f"任务{idx}").strip()
            # 重名会让进度互相踩，自动加后缀区分
            if name in seen:
                seen[name] += 1
                name = f"{name}#{seen[name]}"
                problems.append(f"有重名任务，已改名为 {name}")
            else:
                seen[name] = 0

            messages = item.get("messages")
            if isinstance(messages, str):
                messages = [messages]
            if not isinstance(messages, list):
                problems.append(f"{name} 的 messages 不是数组")
                continue
            messages = [str(m) for m in messages if str(m).strip()]
            if not messages:
                problems.append(f"{name} 没有可用句子")
                continue
            if len(messages) > MAX_MESSAGES_WARN:
                messages = messages[:MAX_MESSAGES_WARN]

            mode = str(item.get("mode") or MODE_ROTATE).strip().lower()
            if mode not in VALID_MODES:
                problems.append(f"{name} 的 mode「{mode}」不认识，按轮播处理")
                mode = MODE_ROTATE

            low, high = parse_every(item.get("every"))

            tasks.append(
                Task(
                    name=name,
                    mode=mode,
                    low=low,
                    high=high,
                    messages=messages,
                    hours=parse_hours(item.get("hours")),
                )
            )

        self.tasks = tasks
        self.enabled = bool(raw.get("enabled", True))
        self.quiet_hours = parse_hours(raw.get("quiet_hours"))
        try:
            self.daily_limit = max(1, int(raw.get("daily_limit", 24)))
        except (TypeError, ValueError):
            self.daily_limit = 24

        groups = raw.get("groups")
        self.group_filter = (
            [str(g).strip() for g in groups if str(g).strip()]
            if isinstance(groups, list)
            else []
        )

        self._mtime = mtime
        self.last_error = ""

        msg = f"已加载 {len(tasks)} 个播报任务 / {sum(len(t.messages) for t in tasks)} 条句子"
        if self.group_filter:
            msg += f"，限定 {len(self.group_filter)} 个群"
        if problems:
            msg += f"（{len(problems)} 处需注意：{'；'.join(problems[:3])}）"
        return True, msg

    def ensure_fresh(self) -> bool:
        """文件改过就重读。返回是否发生了变化。"""
        try:
            if self.path.stat().st_mtime == self._mtime:
                return False
        except OSError:
            return False
        ok, _ = self.load(force=True)
        return ok

    # ---------------- 查询 ----------------
    def find(self, keyword: str) -> Task | None:
        """按名字找任务，支持只写一半/只写几个字。"""
        key = (keyword or "").strip()
        if not key:
            return None
        for task in self.tasks:
            if task.name == key:
                return task
        for task in self.tasks:
            if key in task.name:
                return task
        return None

    def allow_group(self, group_openid: str) -> bool:
        return not self.group_filter or group_openid in self.group_filter

    def pick(self, task: Task, index: int, last_text: str = "") -> tuple[str, int]:
        """按模式挑一句，返回 (句子, 新的轮播下标)。

        rotate 用下标顺序走，到末尾绕回开头；messages 改短了也不会越界。
        random 尽量避开上一句，否则「随机」经常连着出同一条，看着像卡了。
        """
        msgs = task.messages
        if not msgs:
            return "", index

        if task.mode == MODE_RANDOM:
            if len(msgs) == 1:
                return msgs[0], index
            pool = [m for m in msgs if m != last_text] or msgs
            return random.choice(pool), index

        return msgs[index % len(msgs)], (index + 1) % len(msgs)

    def render(self, text: str) -> str:
        """替换 {date} / {time} 占位符。"""
        now = datetime.now()
        return (
            str(text)
            .replace("{date}", now.strftime("%Y-%m-%d"))
            .replace("{time}", now.strftime("%H:%M"))
        )


# ====================================================================
# 进度持久化
# ====================================================================
class StateStore:
    """记「每个任务上次发在什么时候、轮播到第几条」「哪些群」「今天发了几条」。

    为什么不放数据库：这点数据跟用户积分没关系，也不需要跨机器同步，
    一个 JSON 文件够用，还能直接打开看、手工改。
    """

    def __init__(self, path: str | Path | None = None, path_provider=None) -> None:
        self._fixed = Path(path) if path else None
        self._provider = path_provider
        self.groups: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.counters: dict[str, int] = {}
        self._loaded = False
        self._last_flush: float = 0.0

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
        return self._fixed or resolve_path(raw, DEFAULT_STATE)

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            return      # 坏了就当空的，重新记，不影响运行
        if not isinstance(raw, dict):
            return
        for key in ("groups", "tasks", "counters"):
            value = raw.get(key)
            if isinstance(value, dict):
                setattr(self, key, value)

    def flush(self, *, force: bool = False, min_interval: float = 30.0) -> None:
        """落盘。默认 30 秒内不重复写，避免每 30 秒 tick 都动一次磁盘。"""
        now = time.time()
        if not force and now - self._last_flush < min_interval:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "groups": self.groups,
                "tasks": self.tasks,
                "counters": self.counters,
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self.path)
            self._last_flush = now
        except OSError:
            pass

    # ---------------- 群 ----------------
    def touch_group(self, group_openid: str) -> bool:
        """登记一个群。返回是否是第一次见到（新群要立刻落盘）。"""
        now = datetime.now().isoformat(timespec="seconds")
        item = self.groups.get(group_openid)
        if item is None:
            self.groups[group_openid] = {"first_seen": now, "last_seen": now}
            self.flush(force=True)
            return True
        item["last_seen"] = now
        return False

    # ---------------- 任务进度 ----------------
    def task(self, name: str) -> dict:
        return self.tasks.setdefault(name, {})

    def drop_task(self, name: str) -> None:
        """配置里删掉的任务，顺手清掉它的进度，别留垃圾。"""
        self.tasks.pop(name, None)

    # ---------------- 每日计数 ----------------
    @staticmethod
    def _counter_key(day: str, group_openid: str) -> str:
        return f"{day}|{group_openid}"

    def count(self, day: str, group_openid: str) -> int:
        return int(self.counters.get(self._counter_key(day, group_openid), 0) or 0)

    def bump(self, day: str, group_openid: str) -> None:
        key = self._counter_key(day, group_openid)
        self.counters[key] = int(self.counters.get(key, 0) or 0) + 1

    def prune_counter(self, day: str) -> None:
        """不是今天的计数直接扔掉，文件不会无限长大。"""
        prefix = f"{day}|"
        for key in [k for k in self.counters if not k.startswith(prefix)]:
            self.counters.pop(key, None)


def today_str() -> str:
    return date.today().isoformat()


__all__ = [
    "DEFAULT_FILE",
    "DEFAULT_STATE",
    "FeedBook",
    "StateStore",
    "Task",
    "in_window",
    "may_send",
    "parse_every",
    "parse_hours",
    "today_str",
]
