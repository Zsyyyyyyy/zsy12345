#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh_tradable_futures.py —— 刷新 futures_base 合约库「在市合约」（薄壳脚本，文件名沿用历史命名）

抓取逻辑已收拢到 app/routers/data.py 的 refresh_contracts()（与行情看板共用一份新浪抓取代码）。
本脚本只负责命令行入口 + 数据库会话 + 输出格式，供 cron 每日调用。

refresh_contracts 语义（只做两件事，不删历史）：
  ① 新浪当前挂牌、futures_base 里还没有的新合约补进去（is_active=1）；
  ② 本次成功抓取到的交易所里、没再出现的在市合约置 is_active=0（到期下架）。
已退市历史合约（更早年份）由 build_futures_base_history.py 手动补录，不进本脚本维护。

用法（在项目根目录）：
    venv/bin/python refresh_tradable_futures.py            # 拉取 + upsert（人类可读输出）
    venv/bin/python refresh_tradable_futures.py --json     # 只输出 JSON 结果（机器可读）
    venv/bin/python refresh_tradable_futures.py --dry-run  # 只看不写

网页端等价操作（需登录）：
    POST /api/futures/refresh-contracts
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.database import Base, engine, SessionLocal
from app.routers.data import refresh_contracts


def main():
    parser = argparse.ArgumentParser(description='刷新国内期货真实合约字典')
    parser.add_argument('--dry-run', action='store_true', help='只看不写')
    parser.add_argument('--json', action='store_true', help='只输出 JSON 结果（不打印过程行）')
    args = parser.parse_args()

    if not args.dry_run:
        Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        result = refresh_contracts(db, dry_run=args.dry_run,
                                   log=None if args.json else print)
    finally:
        db.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
