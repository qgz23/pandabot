# -*- coding: utf-8 -*-
"""
用户数据存储组件（MySQL / aiomysql 异步版）
================================================================
以「用户ID（session_id）」为主键，持久化到 MySQL。
不依赖 NoneBot，可单独测试、也可被任意插件引用。

连接配置（环境变量，均有本地默认值）：
    DB_HOST     默认 127.0.0.1
    DB_PORT     默认 3306
    DB_USER     默认 root
    DB_PASSWORD 默认 空（本地开发）
    DB_NAME     默认 pandabot

表 users：核心字段独立成列，自定义字段统一放进 extra(JSON) 列。
"""

import json
import os
from datetime import datetime

import aiomysql

# 已知独立列；其余字段写入 extra JSON 列
KNOWN_COLUMNS = {"openid", "nickname", "note", "msg_count", "first_seen", "last_seen"}


class MySQLStore:
    def __init__(self):
        self.host = os.getenv("DB_HOST", "127.0.0.1")
        self.port = int(os.getenv("DB_PORT", "3306"))
        self.user = os.getenv("DB_USER", "root")
        self.password = os.getenv("DB_PASSWORD", "")
        self.db = os.getenv("DB_NAME", "pandabot")
        self._pool = None

    # ---------- 初始化：建连接池 + 建表 ----------
    async def init(self):
        self._pool = await aiomysql.create_pool(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            db=self.db,
            charset="utf8mb4",
            autocommit=True,
            maxsize=5,
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        session_id VARCHAR(128) PRIMARY KEY,
                        openid VARCHAR(128),
                        nickname VARCHAR(128),
                        first_seen DATETIME,
                        last_seen DATETIME,
                        msg_count INT NOT NULL DEFAULT 0,
                        note TEXT,
                        extra JSON,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                                   ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )

                # 兼容旧表：若无 score 列则补加（总积分）
                await cur.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='users'
                      AND COLUMN_NAME='score'
                    """,
                    (self.db,),
                )
                if (await cur.fetchone())[0] == 0:
                    await cur.execute(
                        "ALTER TABLE users ADD COLUMN score INT NOT NULL DEFAULT 0"
                    )

                # 签到记录表：同一用户同一天只允许一条（主键兜底）
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS checkins (
                        session_id VARCHAR(128) NOT NULL,
                        checkin_date DATE NOT NULL,
                        gained INT NOT NULL DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (session_id, checkin_date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )

    async def close(self):
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()

    # ---------- 内部：取一行并把 extra 合并成 dict ----------
    async def _fetch(self, uid: str) -> dict | None:
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM users WHERE session_id=%s", (uid,)
                )
                row = await cur.fetchone()
        if not row:
            return None
        extra = row.get("extra") or "{}"
        if isinstance(extra, str):
            extra = json.loads(extra)
        row.pop("extra", None)
        row.pop("updated_at", None)
        # DATETIME 转字符串便于展示
        for k in ("first_seen", "last_seen"):
            if isinstance(row.get(k), datetime):
                row[k] = row[k].isoformat(timespec="seconds")
        row.update(extra)
        return row

    # ---------- 写入：建档 / 更新，返回是否第一次出现 ----------
    async def touch(self, uid: str, openid: str, nickname: str = "") -> bool:
        existing = await self._fetch(uid)
        now = datetime.now().isoformat(timespec="seconds")
        if existing is None:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO users
                          (session_id, openid, nickname, first_seen, last_seen, msg_count)
                        VALUES (%s, %s, %s, %s, %s, 1)
                        """,
                        (uid, openid, nickname, now, now),
                    )
            return True

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE users
                    SET openid=%s, nickname=COALESCE(NULLIF(%s,''),nickname),
                        last_seen=%s, msg_count=msg_count+1
                    WHERE session_id=%s
                    """,
                    (openid, nickname, now, uid),
                )
        return False

    # ---------- 读取：取某个字段 ----------
    async def get(self, uid: str, field: str, default=None):
        row = await self._fetch(uid)
        if not row:
            return default
        return row.get(field, default)

    # ---------- 读取：取该用户全部数据 ----------
    async def all(self, uid: str) -> dict:
        return await self._fetch(uid) or {}

    # ---------- 写入：写某个字段 ----------
    async def set(self, uid: str, field: str, value):
        if field in ("nickname", "note"):
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"UPDATE users SET {field}=%s WHERE session_id=%s",
                        (value, uid),
                    )
            return
        # 其余自定义字段写入 extra JSON
        row = await self._fetch(uid) or {}
        extra = {k: v for k, v in row.items() if k not in KNOWN_COLUMNS}
        extra[field] = value
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE users SET extra=%s WHERE session_id=%s",
                    (json.dumps(extra, ensure_ascii=False), uid),
                )

    # ---------- 写入：数字字段累加，返回新值 ----------
    async def incr(self, uid: str, field: str, delta: int = 1) -> int:
        if field == "msg_count":
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE users SET msg_count=msg_count+%s WHERE session_id=%s",
                        (delta, uid),
                    )
            return await self.get(uid, "msg_count", 0)

        # extra 内数字字段
        row = await self._fetch(uid) or {}
        extra = {k: v for k, v in row.items() if k not in KNOWN_COLUMNS}
        new_val = int(extra.get(field, 0)) + delta
        extra[field] = new_val
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE users SET extra=%s WHERE session_id=%s",
                    (json.dumps(extra, ensure_ascii=False), uid),
                )
        return new_val

    # ---------- 积分：查询当前总积分 ----------
    async def get_score(self, uid: str) -> int:
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT score FROM users WHERE session_id=%s", (uid,)
                )
                row = await cur.fetchone()
        return int(row["score"]) if row else 0

    # ---------- 签到：每日一次，成功则加积分 ----------
    async def checkin(self, uid: str, date_str: str, gain: int = 10) -> tuple[bool, int]:
        """返回 (是否本次新签到, 当前总积分)。当天已签返回 (False, 当前分)。"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM checkins WHERE session_id=%s AND checkin_date=%s",
                    (uid, date_str),
                )
                if await cur.fetchone():
                    return False, await self.get_score(uid)

                # 插入签到记录 + 累加总分（主键唯一兜底重复）
                await cur.execute(
                    "INSERT INTO checkins (session_id, checkin_date, gained) "
                    "VALUES (%s, %s, %s)",
                    (uid, date_str, gain),
                )
                await cur.execute(
                    "UPDATE users SET score=score+%s WHERE session_id=%s",
                    (gain, uid),
                )
        return True, await self.get_score(uid)


# ===== 全局路由单例：自动选择后端 =====
# 配了 DB_HOST 且能连上 → MySQL；否则零安装退回本地 SQLite 文件。
from nonebot.log import logger

from .sqlite_store import SQLiteStore


class StoreRouter:
    def __init__(self):
        self.backend = None
        self.kind = None

    async def init(self):
        # DB_HOST=none 显式禁用 MySQL，直接用 SQLite
        if os.getenv("DB_HOST", "").strip().lower() == "none":
            self._use_sqlite()
            return
        try:
            mysql = MySQLStore()
            await mysql.init()
            self.backend = mysql
            self.kind = "mysql"
            logger.info(f"数据库后端：MySQL ({mysql.host}:{mysql.port}/{mysql.db})")
            return
        except Exception as e:  # 连不上则回退，不阻断启动
            logger.warning(f"MySQL 不可用，回退到 SQLite：{e!r}", exc_info=True)

        self._use_sqlite()

    def _use_sqlite(self):
        self.backend = SQLiteStore()
        self.backend.init()
        self.kind = "sqlite"
        logger.info(f"数据库后端：SQLite 文件 {self.backend.path}")

    async def close(self):
        close = getattr(self.backend, "close", None)
        if close:
            res = close()
            if hasattr(res, "__await__"):
                await res

    async def touch(self, uid, openid, nickname=""):
        return self.backend.touch(uid, openid, nickname)

    async def get(self, uid, field, default=None):
        return self.backend.get(uid, field, default)

    async def all(self, uid):
        return self.backend.all(uid)

    async def set(self, uid, field, value):
        self.backend.set(uid, field, value)

    async def incr(self, uid, field, delta=1):
        return self.backend.incr(uid, field, delta)

    async def get_score(self, uid):
        return self.backend.get_score(uid)

    async def checkin(self, uid, date_str, gain=10):
        return self.backend.checkin(uid, date_str, gain)


# 插件 startup 时调用 await store.init()
store = StoreRouter()
