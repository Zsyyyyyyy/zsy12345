#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_daily_history.py —— 抓取国内期货「具体合约」日级历史行情入库（薄壳脚本）

抓取逻辑已收拢到 app/routers/data.py 的 run_fetch_history()（与行情看板共用一份新浪抓取代码）。
本脚本只负责命令行入口 + 数据库会话 + 输出格式，供 cron 每日调用。

用法（在项目根目录）：
    venv/bin/python fetch_daily_history.py                       # 全部具体合约
    venv/bin/python fetch_daily_history.py --symbols RB2701,CU0  # 指定 symbol
    venv/bin/python fetch_daily_history.py --limit 10            # 先试 10 个
    venv/bin/python fetch_daily_history.py --active-only         # 只抓在市合约
    venv/bin/python fetch_daily_history.py --full-refresh        # 全量重刷
    venv/bin/python fetch_daily_history.py --dry-run             # 只抓不写
    venv/bin/python fetch_daily_history.py --json                # 只输出 JSON 结果

网页端等价操作（需登录）：
    POST /api/futures/fetch-history
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.database import Base, engine, SessionLocal
from app.routers.history import run_fetch_history


def main():
    parser = argparse.ArgumentParser(description='抓取国内期货具体合约日级历史行情入库')
    parser.add_argument('--symbols', default='',
                        help='逗号分隔的 symbol 列表（如 RB2701,RB0），缺省=futures_base 全部具体合约')
    parser.add_argument('--active-only', action='store_true',
                        help='只抓 is_active=1 的在市合约（默认含已下架合约，历史更全）')
    parser.add_argument('--limit', type=int, default=0, help='只处理前 N 个合约（试跑用）')
    parser.add_argument('--full-refresh', action='store_true',
                        help='忽略增量起点，全量 upsert（表结构变更/数据修复时用）')
    parser.add_argument('--sleep', type=float, default=0.3, help='请求间隔秒数')
    parser.add_argument('--dry-run', action='store_true', help='只抓取解析，不写库')
    parser.add_argument('--json', action='store_true', help='只输出 JSON 结果（不打印过程行）')
    args = parser.parse_args()

    if not args.dry_run:
        Base.metadata.create_all(bind=engine)  # 自动建表（已存在则无副作用）

    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]

    db = SessionLocal()
    try:
        result = run_fetch_history(
            db,
            symbols=symbols,
            active_only=args.active_only,
            limit=args.limit,
            full_refresh=args.full_refresh,
            sleep_s=args.sleep,
            dry_run=args.dry_run,
            log=None if args.json else print,
        )
    finally:
        db.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
