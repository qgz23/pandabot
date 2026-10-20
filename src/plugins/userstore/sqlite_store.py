# -*- coding: utf-8 -*-
"""
SQLite 后端（零安装兜底，数据落本地文件）
================================================================
接口与 MySQLStore 完全对齐；标准库 sqlite3，单连接 + 锁，同步调用。
文件路径默认项目根 data/pandabot.db，可用环境变量 SQLITE_PATH 覆盖。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime

KNOWN_COLUMNS = {"openid", "nickname", "note", "msg_count", "first_seen", "last_seen"}


class SQLiteStore:
    def __init__(self):
        self.path = os.getenv("SQLITE_PATH") or os.path.join(
            os.getcwd(), "data", "pandabot.db"
        )
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ---------- 初始化：建表 ----------
    def init(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    session_id TEXT PRIMARY KEY,
                    openid TEXT,
                    nickname TEXT,
                    first_seen TEXT,
                    last_seen TEXT,
                    msg_count INTEGER NOT NULL DEFAULT 0,
                    note TEXT,
                    score INTEGER NOT NULL DEFAULT 0,
                    extra TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkins (
                    session_id TEXT NOT NULL,
                    checkin_date TEXT NOT NULL,
                    gained INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (session_id, checkin_date)
                )
                """
            )

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ---------- 内部：取一行并合并 extra ----------
    def _fetch(self, uid: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM users WHERE session_id=?", (uid,)
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        data = dict(zip(cols, row))
        extra = data.pop("extra", None) or "{}"
        if isinstance(extra, str):
            extra = json.loads(extra)
        data.update(extra)
        return data

    # ---------- 写入：建档 / 更新，返回是否第一次出现 ----------
    def touch(self, uid: str, openid: str, nickname: str = "") -> bool:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            exists = self._conn.execute(
                "SELECT 1 FROM users WHERE session_id=?", (uid,)
            ).fetchone()
            if not exists:
                self._conn.execute(
                    "INSERT INTO users (session_id,openid,nickname,first_seen,last_seen,msg_count) "
                    "VALUES (?,?,?,?,?,1)",
                    (uid, openid, nickname, now, now),
                )
                return True
            self._conn.execute(
                "UPDATE users SET openid=?, last_seen=?, "
                "nickname=CASE WHEN ?='' THEN nickname ELSE ? END, "
                "msg_count=msg_count+1 WHERE session_id=?",
                (openid, now, nickname, nickname, uid),
            )
        return False

    # ---------- 读取：取某字段 ----------
    def get(self, uid: str, field: str, default=None):
        row = self._fetch(uid)
        return row.get(field, default) if row else default

    # ---------- 读取：全部 ----------
    def all(self, uid: str) -> dict:
        return self._fetch(uid) or {}

    # ---------- 写入：某字段 ----------
    def set(self, uid: str, field: str, value):
        with self._lock, self._conn:
            if field in ("nickname", "note"):
                self._conn.execute(
                    f"UPDATE users SET {field}=? WHERE session_id=?",
                    (value, uid),
                )
                return
            row = self._fetch(uid) or {}
            extra = {k: v for k, v in row.items() if k not in KNOWN_COLUMNS}
            extra[field] = value
            self._conn.execute(
                "UPDATE users SET extra=? WHERE session_id=?",
                (json.dumps(extra, ensure_ascii=False), uid),
            )

    # ---------- 写入：数字累加，返回新值 ----------
    def incr(self, uid: str, field: str, delta: int = 1) -> int:
        with self._lock, self._conn:
            if field == "msg_count":
                self._conn.execute(
                    "UPDATE users SET msg_count=msg_count+? WHERE session_id=?",
                    (delta, uid),
                )
                return self.get(uid, "msg_count", 0)
            row = self._fetch(uid) or {}
            extra = {k: v for k, v in row.items() if k not in KNOWN_COLUMNS}
            new_val = int(extra.get(field, 0)) + delta
            extra[field] = new_val
            self._conn.execute(
                "UPDATE users SET extra=? WHERE session_id=?",
                (json.dumps(extra, ensure_ascii=False), uid),
            )
        return new_val

    # ---------- 积分：查总分 ----------
    def get_score(self, uid: str) -> int:
        row = self._conn.execute(
            "SELECT score FROM users WHERE session_id=?", (uid,)
        ).fetchone()
        return int(row[0]) if row else 0

    # ---------- 签到：每日一次，成功加积分 ----------
    def checkin(self, uid: str, date_str: str, gain: int = 10) -> tuple[bool, int]:
        with self._lock, self._conn:
            done = self._conn.execute(
                "SELECT 1 FROM checkins WHERE session_id=? AND checkin_date=?",
                (uid, date_str),
            ).fetchone()
            if done:
                return False, self.get_score(uid)
            self._conn.execute(
                "INSERT INTO checkins (session_id,checkin_date,gained) VALUES (?,?,?)",
                (uid, date_str, gain),
            )
            self._conn.execute(
                "UPDATE users SET score=score+? WHERE session_id=?",
                (gain, uid),
            )
        return True, self.get_score(uid)
