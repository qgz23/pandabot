# -*- coding: utf-8 -*-
"""
云数据库连通性检查
================================================================
部署脚本在装完依赖后调它，也能单独跑：

    .venv\\Scripts\\python.exe deploy\\check-db.py

为什么要单独做一个检查：
    store.py 的设计是「MySQL 连不上就降级用本地 SQLite」。这个降级很贴心，
    但也意味着 —— 服务器上如果 Aiven 的 IP 白名单没加服务器公网 IP，
    机器人照样正常跑、照样能签到、照样攒积分，数据只是悄悄写进了
    C:\\pandabot\\data\\pandabot.db。你和搭档两边都以为在同一个库里。

    等发现的时候已经攒了几百条对不上的记录。所以部署时就先测一次。

输出约定：
    第一行是机器可读的标记，部署脚本用它判断结果
        [DB-OK]      连上了
        [DB-SKIP]    没配置数据库，将使用本地 SQLite
        [DB-FAIL]    配了但连不上
    后面几行给人看。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# 项目根：deploy/ 的上一级
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.prod"

# .env.prod 里读这几个键，和 userstore/store.py 用的名字保持一致
KEYS = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")


def parse_env(path: Path) -> dict[str, str]:
    """只解析 KEY=VALUE，够用。不用 python-dotenv 是为了少一个依赖。"""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 去掉引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[key] = value
    return data


def explain(exc: BaseException) -> list[str]:
    """把 pymysql / aiomysql 的异常翻成能直接照做的建议。"""
    text = str(exc)
    code = getattr(exc, "args", [None])[0] if exc.args else None
    tips: list[str] = []

    if code == 2003 or "Can't connect" in text:
        tips.append("连不到主机和端口。三种可能，按顺序排查：")
        tips.append("  1. 白名单没加服务器 IP —— Aiven 控制台 → 你的服务 → Allowed IP addresses")
        tips.append("     服务器公网 IP 可以在服务器上执行：curl ifconfig.me")
        tips.append("  2. 服务器出站被防火墙拦了（云厂商的安全组只管入站，一般不是这个）")
        tips.append("  3. DB_HOST / DB_PORT 填错，注意 Aiven 的端口不是 3306")
    elif code == 1045 or "Access denied" in text:
        tips.append("账号密码不对。检查 .env.prod 里的 DB_USER / DB_PASSWORD，")
        tips.append("注意密码里如果有 # 或空格，需要用引号包起来。")
    elif code == 1049 or "Unknown database" in text:
        tips.append("库名不对。Aiven 免费套餐默认库叫 defaultdb，检查 DB_NAME。")
    elif "timed out" in text.lower() or "timeout" in text.lower():
        tips.append("连接超时。最常见就是白名单没加服务器 IP，请求被静默丢弃。")
    else:
        tips.append("未识别的错误，原始信息见上。")

    return tips


async def check(cfg: dict[str, str]) -> int:
    host = cfg.get("DB_HOST") or ""
    if not host:
        print("[DB-SKIP]")
        print(".env.prod 里没有 DB_HOST，机器人会用本地 SQLite（data/pandabot.db）。")
        print("如果这是故意的（单机测试）就没问题；要连云库就补上下面几项：")
        print("  DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME")
        return 0

    port_raw = cfg.get("DB_PORT") or "3306"
    try:
        port = int(port_raw)
    except ValueError:
        print("[DB-FAIL]")
        print(f"DB_PORT 不是数字：{port_raw!r}")
        return 1

    user = cfg.get("DB_USER") or ""
    password = cfg.get("DB_PASSWORD") or ""
    dbname = cfg.get("DB_NAME") or ""

    print(f"目标：{host}:{port}  用户 {user or '(空)'}  库 {dbname or '(未填)'}")

    try:
        import aiomysql
    except ImportError:
        print("[DB-FAIL]")
        print("venv 里没有 aiomysql，先装依赖：")
        print(r"  .venv\Scripts\python.exe -m pip install aiomysql")
        return 1

    started = time.monotonic()
    try:
        conn = await asyncio.wait_for(
            aiomysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                db=dbname or None,
                connect_timeout=12,
            ),
            timeout=20,
        )
    except asyncio.TimeoutError:
        print("[DB-FAIL]")
        print("连接超时（20 秒）。")
        for line in explain(OSError("timed out")):
            print(line)
        return 1
    except Exception as exc:  # noqa: BLE001 —— 这里就是要兜住所有连接异常
        print("[DB-FAIL]")
        print(f"{type(exc).__name__}: {exc}")
        print("")
        for line in explain(exc):
            print(line)
        return 1

    # 连上了，再看一眼版本和表数量，确认是那个库
    try:
        cur = await conn.cursor()
        await cur.execute("SELECT VERSION(), DATABASE()")
        row = await cur.fetchone()
        version, current_db = (row[0], row[1]) if row else ("?", "?")

        await cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE()"
        )
        row2 = await cur.fetchone()
        tables = row2[0] if row2 else 0
        await cur.close()
    finally:
        conn.close()

    elapsed = (time.monotonic() - started) * 1000
    print("[DB-OK]")
    print(f"MySQL 版本 : {version}")
    print(f"当前库     : {current_db}")
    print(f"表数量     : {tables}")
    print(f"连接耗时   : {elapsed:.0f} ms")

    if tables == 0:
        print("")
        print("注意：这个库里一张表都没有。首次启动时 store.py 会自动建表，属正常。")
    return 0


def main() -> int:
    if not ENV_FILE.exists():
        print("[DB-FAIL]")
        print(f"找不到 {ENV_FILE}")
        return 1

    cfg = parse_env(ENV_FILE)
    if "DB_HOST" not in cfg:
        # 环境变量也可能直接给（nb run 时 load_dotenv 会塞进 os.environ）
        for key in KEYS:
            if os.getenv(key):
                cfg[key] = os.environ[key]

    return asyncio.run(check(cfg))


if __name__ == "__main__":
    sys.exit(main())
