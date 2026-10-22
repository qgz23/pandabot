# -*- coding: utf-8 -*-
"""
把本地 SQLite 的数据合并进云 MySQL
================================================================
用途：云库被暂停/删库期间，bot 会静默降级写 SQLite。
云库恢复后，把这段时间的数据搬回云端，否则两边的数据各活各的
（用户在群里看积分，一人一个数）。

设计原则：**只合并不覆盖**，可以重复执行。
    - 按 session_id 对齐，两边都没有的才新增
    - 同一用户在两边都有时：
        first_seen 取更早、last_seen 取更晚、msg_count / score 取更大
        nickname 优先用非空的新值
        extra JSON 做浅合并（本地覆盖云端同名键）
    - 签到记录按 (session_id, checkin_date) 主键去重，重复的直接跳过

不修改 SQLite 文件（只读），所以跑多少遍都不会丢本地数据。

关于 score 的一个取舍：两边都有该用户时取 **max 而不是相加**。
相加会在重复执行时把分数翻倍；而 max 的代价是「云端恢复后又在两端各涨了一部分」
这种极端情况下会少算。比起分数凭空膨胀，宁可少算——少算能人工补，翻倍会把体系搞崩。

用法（在 project 根目录）：
    python scripts/migrate_sqlite_to_mysql.py            # 只打印计划，不写入
    python scripts/migrate_sqlite_to_mysql.py --apply    # 真正写入
    python scripts/migrate_sqlite_to_mysql.py --apply --sqlite data/pandabot.db

配置从 .env.prod 里的 DB_* 读取（也可直接设环境变量）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sqlite3
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent

# extra JSON 里不该出现的键：它们是独立列，重复放一份会让「积分到底哪个是真的」变成玄学。
# （老版本 KNOWN_COLUMNS 漏了 session_id / score，历史数据里可能已经混进去了）
NOISE_KEYS = {
    "session_id", "openid", "nickname", "note",
    "msg_count", "score", "first_seen", "last_seen",
    "extra", "updated_at",
}


def load_env() -> None:
    """从 .env.prod 补 DB_* 环境变量（不覆盖已有值）。"""
    env_file = ROOT / ".env.prod"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("DB_") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def parse_dt(value) -> datetime | None:
    """SQLite 里存的是 ISO 字符串，MySQL 要 datetime 对象。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("T", " ").split(".")[0].rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def clean_extra(raw) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k not in NOISE_KEYS}


def read_sqlite(path: pathlib.Path) -> tuple[list[dict], list[dict]]:
    if not path.exists():
        sys.exit(f"找不到 SQLite 文件：{path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    users = [dict(r) for r in con.execute("SELECT * FROM users")]
    try:
        checkins = [dict(r) for r in con.execute("SELECT * FROM checkins")]
    except sqlite3.OperationalError:
        checkins = []
    con.close()
    return users, checkins


def merge_user(local: dict, remote: dict | None) -> dict:
    """把本地行合并到云端行上，返回最终要写的字段。"""
    local = dict(local)
    local["extra"] = clean_extra(local.get("extra"))
    if remote is None:
        return local

    extra = clean_extra(remote.get("extra"))
    extra.update(local["extra"])  # 本地覆盖同名键

    def later(a, b):
        a, b = parse_dt(a), parse_dt(b)
        if a and b:
            return max(a, b)
        return a or b

    def greater(a, b):
        try:
            return max(int(a or 0), int(b or 0))
        except (TypeError, ValueError):
            return a or b

    return {
        "session_id": local["session_id"],
        "openid": local.get("openid") or remote.get("openid"),
        "nickname": local.get("nickname") or remote.get("nickname"),
        "first_seen": later(remote.get("first_seen"), local.get("first_seen")),
        "last_seen": later(remote.get("last_seen"), local.get("last_seen")),
        "msg_count": greater(local.get("msg_count"), remote.get("msg_count")),
        "note": local.get("note") or remote.get("note"),
        "score": greater(local.get("score"), remote.get("score")),
        "extra": extra,
    }


async def run(apply: bool, sqlite_path: pathlib.Path) -> None:
    import aiomysql

    users, checkins = read_sqlite(sqlite_path)
    print(f"本地 SQLite：{sqlite_path}")
    print(f"  users {len(users)} 行 / checkins {len(checkins)} 行\n")

    pool = await aiomysql.create_pool(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        db=os.environ["DB_NAME"],
        charset="utf8mb4",
        autocommit=True,
        maxsize=3,
        ssl={} if os.environ.get("DB_SSL", "") == "1" else None,
        connect_timeout=15,
    )

    added = updated = 0
    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                for row in users:
                    sid = row["session_id"]
                    await cur.execute(
                        "SELECT * FROM users WHERE session_id=%s", (sid,)
                    )
                    remote = await cur.fetchone()
                    final = merge_user(row, remote)
                    nick = final.get("nickname") or ""

                    if remote is None:
                        added += 1
                        print(f"  + 新增 {nick:<10} {sid[-8:]}  "
                              f"score={final.get('score') or 0}")
                        if apply:
                            await cur.execute(
                                "INSERT INTO users (session_id, openid, nickname,"
                                " first_seen, last_seen, msg_count, note, score, extra)"
                                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                (
                                    sid, final.get("openid"), nick,
                                    final.get("first_seen"), final.get("last_seen"),
                                    int(final.get("msg_count") or 0),
                                    final.get("note"), int(final.get("score") or 0),
                                    json.dumps(final["extra"], ensure_ascii=False),
                                ),
                            )
                    else:
                        updated += 1
                        changed = (
                            final["msg_count"] != (remote.get("msg_count") or 0)
                            or final["score"] != (remote.get("score") or 0)
                            or final["nickname"] != remote.get("nickname")
                        )
                        print(f"  ~ 更新 {nick:<10} {sid[-8:]}  "
                              f"score {remote.get('score')}→{final['score']}  "
                              f"msg {remote.get('msg_count')}→{final['msg_count']}"
                              f"{'' if changed else '（无变化）'}")
                        if apply:
                            await cur.execute(
                                "UPDATE users SET openid=%s, nickname=%s,"
                                " first_seen=%s, last_seen=%s, msg_count=%s,"
                                " note=%s, score=%s, extra=%s WHERE session_id=%s",
                                (
                                    final.get("openid"), nick,
                                    final.get("first_seen"), final.get("last_seen"),
                                    int(final.get("msg_count") or 0),
                                    final.get("note"), int(final.get("score") or 0),
                                    json.dumps(final["extra"], ensure_ascii=False),
                                    sid,
                                ),
                            )

                cin_added = 0
                for rec in checkins:
                    if apply:
                        await cur.execute(
                            "INSERT IGNORE INTO checkins"
                            " (session_id, checkin_date, gained) VALUES (%s,%s,%s)",
                            (
                                rec["session_id"],
                                parse_dt(rec.get("checkin_date")),
                                int(rec.get("gained") or 0),
                            ),
                        )
                        cin_added += cur.rowcount
                    else:
                        cin_added += 1
                print(f"\n  checkins {'导入' if apply else '待导入'} {cin_added} 行")

                if apply:
                    await cur.execute("SELECT COUNT(*) FROM users")
                    print(f"写入后云端 users 共 {(await cur.fetchone())['COUNT(*)']} 行")
                    await cur.execute("SELECT COUNT(*) FROM checkins")
                    print(f"写入后云端 checkins 共 {(await cur.fetchone())['COUNT(*)']} 行")
    finally:
        pool.close()
        await pool.wait_closed()

    mode = "已写入" if apply else "预演（未写入）"
    print(f"\n{mode}：新增 {added} / 更新 {updated}")
    if not apply:
        print("确认无误后加 --apply 再跑一次")


def main() -> None:
    ap = argparse.ArgumentParser(description="SQLite → MySQL 数据合并")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    ap.add_argument(
        "--sqlite", default=str(ROOT / "data" / "pandabot.db"),
        help="SQLite 文件路径",
    )
    args = ap.parse_args()

    load_env()
    for key in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        if not os.environ.get(key):
            sys.exit(f"缺少环境变量 {key}（检查 .env.prod）")

    asyncio.run(run(args.apply, pathlib.Path(args.sqlite)))


if __name__ == "__main__":
    main()
