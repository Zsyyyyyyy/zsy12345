#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh_tradable_futures.py —— 定时刷新 futures_base 合约库（薄壳脚本，文件名沿用历史命名）

抓取逻辑收拢在 app/routers/history.py 的 refresh_contracts()（与旧版共用同一份新浪抓取代码）。
本脚本只负责命令行入口 + 数据库会话 + 输出格式，供 cron 每日调用。

refresh_contracts 语义（只加不删，无 is_active）：
  ① 新浪当前挂牌、futures_base 里还没有的合约补进去（幂等 upsert，同时刷新名称/乘数字段）；
  ② 已入库的合约（含到期后不再挂牌的）一律保留、不改状态。
是否「当前可交易」由查询方按 symbol 交割年月判断（交割月 >= 当前月），
因此本脚本不需要维护 is_active、也不做下架扫描。

用法（在项目根目录）：
    venv/bin/python refresh_tradable_futures.py            # 拉取 + upsert（人类可读输出）
    venv/bin/python refresh_tradable_futures.py --json     # 只输出 JSON 结果（机器可读）
    venv/bin/python refresh_tradable_futures.py --dry-run  # 只看不写
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.database import Base, engine, SessionLocal
from app.routers.history import refresh_contracts


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
