# -*- coding: utf-8 -*-
"""
业务配置热加载（零依赖轮询版）
================================================================
修改 settings.json 后无需重启进程，约 3 秒内自动生效，QQ 长连接不断。

其他插件用法：
    from src.plugins.settings.config import DATA, get
    gain = get("checkin_gain", 10)
    enabled = get("features.checkin_enabled", True)

两个必须遵守的约定：
    1. 只允许"原地更新" DATA（clear + update），不能重新赋值。
       因为别的模块 import 的是同一个字典对象的引用，重新赋值会让它们看不见变化。
    2. 使用时现读 DATA，不要在 import 阶段抄成模块级常量，
       否则热重载对它无效。
"""

import asyncio
import json
from pathlib import Path

from nonebot.log import logger

CONFIG_PATH = Path(__file__).parent / "settings.json"

# 模块级可变容器：所有插件共享同一个对象引用
DATA: dict = {}

# 上次成功加载时的文件修改时间，用于判断是否需要重载
_last_mtime: float = 0.0


def load_now() -> bool:
    """从磁盘重新读取配置，原地更新 DATA。失败时保留旧配置，不影响运行。"""
    global _last_mtime
    try:
        parsed = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("settings.json 顶层必须是 JSON 对象 { }")
    except FileNotFoundError:
        logger.warning(f"配置文件不存在：{CONFIG_PATH}")
        return False
    except Exception as e:
        logger.warning(f"配置解析失败，继续沿用旧配置：{e!r}")
        return False

    DATA.clear()
    DATA.update(parsed)
    _last_mtime = CONFIG_PATH.stat().st_mtime
    return True


def get(key: str, default=None):
    """读取配置项，支持点号路径，例如 get("features.checkin_enabled")。"""
    if "." in key:
        cur = DATA
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur
    return DATA.get(key, default)


async def watch_loop(interval: float = 3.0):
    """轮询文件修改时间，发现变化就重载。只换内存值，不重启进程。"""
    while True:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
            if mtime != _last_mtime and load_now():
                logger.info(f"配置已热重载：{CONFIG_PATH.name}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"配置轮询异常：{e!r}")
        await asyncio.sleep(interval)


# 模块首次被导入时先加载一次，保证启动即有值
load_now()
