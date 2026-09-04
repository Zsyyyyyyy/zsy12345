#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量数据库迁移执行器 —— 无需 Alembic，纯 pymysql，版本化 + 可回滚 + 防并发。

设计目标：让「改表结构」变成一条可重复、可追溯、可回滚的命令，
从而在发布流程里做到：先跑 DDL（Expand），再发代码（Migrate），出问题能回滚（Contract）。

命令：
  ./venv/bin/python migrate.py status              查看哪些迁移已应用 / 待应用
  ./venv/bin/python migrate.py up [--dry-run]      应用所有待执行的迁移
  ./venv/bin/python migrate.py down <version>      回滚指定迁移（执行同名 .down.sql）
  ./venv/bin/python migrate.py mark <version>      只标记为已应用，不执行（存量库基线用）

迁移文件约定：
  migrations/<version>.sql         升级脚本，version 建议 YYYYMMDD_NN_描述
  migrations/<version>.down.sql    回滚脚本（可选，没有则不允许 down）

已有库接入（重要）：
  如果某个迁移其实已经在库里手工跑过了，用 mark 标记，不要重复执行：
    ./venv/bin/python migrate.py mark 20260903_01_add_position_direction

注意事项：
  1. MySQL 的 DDL 会隐式提交事务，所以「执行 SQL」和「记录版本」不是一个原子操作。
     若执行成功但记录失败，下次会重复执行 —— 因此升级脚本尽量写成幂等或可容忍重复
     （例如加列前先判断）。执行器已用 GET_LOCK 防止并发下重复执行。
  2. SQL 切分是简化实现：按分号切，跳过 -- 和 /* */ 注释。
     够覆盖绝大多数 DDL；若脚本里字符串字面量含分号，请改用 mysql 客户端执行。
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pymysql
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
MIGRATIONS_DIR = ROOT / "migrations"
LOCK_NAME = "zsy12345_schema_migrate"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://root:123456@127.0.0.1:3306/fastapi_auth",
)


# ---------- 数据库连接 ----------

def parse_url(url: str) -> dict:
    """把 mysql+pymysql://user:pwd@host:port/db 解析成 pymysql 连接参数。"""
    after_scheme = url.split("://", 1)[-1]
    creds, rest = after_scheme.split("@", 1)
    user, password = creds.split(":", 1)
    hostport, database = rest.split("/", 1)
    database = database.split("?")[0]
    if ":" in hostport:
        host, port = hostport.split(":", 1)
    else:
        host, port = hostport, "3306"
    return dict(
        host=host, port=int(port), user=user,
        password=password, database=database,
        charset="utf8mb4", autocommit=False,
    )


def connect():
    return pymysql.connect(**parse_url(DATABASE_URL))


# ---------- SQL 解析 ----------

def split_sql(text: str) -> list:
    """去掉注释后按分号切分成单条语句列表。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("--") or s.startswith("#"):
            continue
        kept.append(line)
    return [stmt.strip() for stmt in "\n".join(kept).split(";") if stmt.strip()]


# ---------- 版本表 ----------

def ensure_meta_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version    VARCHAR(64)  NOT NULL PRIMARY KEY,
              name       VARCHAR(255) NOT NULL,
              applied_at DATETIME     NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    conn.commit()


def applied_versions(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version")
        return {r[0]: r[1] for r in cur.fetchall()}


def discover_migrations() -> list:
    """扫描 migrations/*.sql，返回 [(version, path)]，按版本号排序。"""
    items = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if p.name.endswith(".down.sql"):
            continue
        items.append((p.stem, p))
    return items


def acquire_lock(conn, timeout: int = 10) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, %s)", (LOCK_NAME, timeout))
        return cur.fetchone()[0] == 1


def release_lock(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))


# ---------- 子命令 ----------

def cmd_status(args):
    conn = connect()
    try:
        ensure_meta_table(conn)
        applied = applied_versions(conn)
        all_mig = discover_migrations()

        print(f"数据库：{parse_url(DATABASE_URL)['database']} @ "
              f"{parse_url(DATABASE_URL)['host']}:{parse_url(DATABASE_URL)['port']}")
        print(f"迁移目录：{MIGRATIONS_DIR}")
        print()
        if not all_mig:
            print("（未发现任何迁移文件）")
            return

        pending = [(v, p) for v, p in all_mig if v not in applied]
        print(f"{'状态':<6} {'版本':<46} 应用时间")
        print("-" * 78)
        for v, _ in all_mig:
            if v in applied:
                ts = applied[v].strftime("%Y-%m-%d %H:%M:%S") if hasattr(applied[v], "strftime") else str(applied[v])
                print(f"{'已应用':<7} {v:<46} {ts}")
            else:
                print(f"{'待应用':<7} {v:<46} -")
        print("-" * 78)
        print(f"共 {len(all_mig)} 个迁移，{len(applied)} 个已应用，{len(pending)} 个待应用")
    finally:
        conn.close()


def cmd_up(args):
    conn = connect()
    try:
        if not acquire_lock(conn):
            print(f"✗ 无法获取迁移锁 {LOCK_NAME}（可能有其他实例正在迁移）")
            sys.exit(1)
        ensure_meta_table(conn)
        applied = applied_versions(conn)
        pending = [(v, p) for v, p in discover_migrations() if v not in applied]

        if not pending:
            print("✓ 已是最新，无需迁移")
            return

        print(f"待应用 {len(pending)} 个迁移：")
        for v, _ in pending:
            print(f"  ↑ {v}")

        if args.dry_run:
            print("\n[dry-run] 未实际执行。以下是将要执行的语句：")
            for v, p in pending:
                print(f"\n── {v} ──")
                for s in split_sql(p.read_text(encoding="utf-8")):
                    print("  " + " ".join(s.split())[:110])
            return

        print()
        for v, p in pending:
            print(f"▶ 应用 {v}")
            try:
                with conn.cursor() as cur:
                    for stmt in split_sql(p.read_text(encoding="utf-8")):
                        print("   " + " ".join(stmt.split())[:110])
                        cur.execute(stmt)
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, %s)",
                        (v, p.name, datetime.now()),
                    )
                conn.commit()
                print(f"  ✓ {v} 完成\n")
            except Exception as e:
                conn.rollback()
                print(f"\n✗ {v} 失败：{e}")
                print("  已应用的部分不会回滚，请手工检查库状态后处理。")
                sys.exit(1)
        print("全部迁移完成")
    finally:
        release_lock(conn)
        conn.close()


def cmd_down(args):
    conn = connect()
    try:
        if not acquire_lock(conn):
            print(f"✗ 无法获取迁移锁 {LOCK_NAME}")
            sys.exit(1)
        ensure_meta_table(conn)
        applied = applied_versions(conn)

        if args.version not in applied:
            print(f"✗ {args.version} 未标记为已应用，无需回滚")
            sys.exit(1)

        down_file = MIGRATIONS_DIR / f"{args.version}.down.sql"
        if not down_file.exists():
            print(f"✗ 缺少回滚脚本：{down_file}")
            print("  请手工编写后再执行，或确认该迁移确实无需回滚。")
            sys.exit(1)

        print(f"▼ 回滚 {args.version}")
        if args.dry_run:
            print("[dry-run] 将要执行的语句：")
            for s in split_sql(down_file.read_text(encoding="utf-8")):
                print("  " + " ".join(s.split())[:110])
            return

        if not args.yes:
            confirm = input("  高危操作，确认回滚？（输入 yes 继续） ")
            if confirm.strip().lower() != "yes":
                print("已取消")
                return

        try:
            with conn.cursor() as cur:
                for stmt in split_sql(down_file.read_text(encoding="utf-8")):
                    print("   " + " ".join(stmt.split())[:110])
                    cur.execute(stmt)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM schema_migrations WHERE version = %s", (args.version,))
            conn.commit()
            print(f"  ✓ {args.version} 已回滚")
        except Exception as e:
            conn.rollback()
            print(f"\n✗ 回滚失败：{e}")
            sys.exit(1)
    finally:
        release_lock(conn)
        conn.close()


def cmd_mark(args):
    """把存量库里已经手工执行过的迁移标记为已应用，避免重复执行。"""
    conn = connect()
    try:
        ensure_meta_table(conn)
        applied = applied_versions(conn)
        name = f"{args.version}.sql"
        if args.version in applied:
            print(f"· {args.version} 已标记过，跳过")
            return
        if not (MIGRATIONS_DIR / name).exists():
            print(f"✗ 找不到迁移文件 {name}")
            sys.exit(1)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, %s)",
                (args.version, name, datetime.now()),
            )
        conn.commit()
        print(f"✓ {args.version} 已标记为已应用（未执行 SQL）")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="轻量数据库迁移执行器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看迁移状态").set_defaults(func=cmd_status)

    p_up = sub.add_parser("up", help="应用所有待执行迁移")
    p_up.add_argument("--dry-run", action="store_true", help="只打印不执行")
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="回滚指定迁移")
    p_down.add_argument("version", help="迁移版本号（文件名去扩展名）")
    p_down.add_argument("--yes", action="store_true", help="跳过确认")
    p_down.add_argument("--dry-run", action="store_true", help="只打印不执行")
    p_down.set_defaults(func=cmd_down)

    p_mark = sub.add_parser("mark", help="标记为已应用（存量库基线）")
    p_mark.add_argument("version", help="迁移版本号")
    p_mark.set_defaults(func=cmd_mark)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
