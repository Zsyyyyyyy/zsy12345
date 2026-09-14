#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh_futures_base.py —— 定时刷新 futures_base 合约库（薄壳脚本）

原名 refresh_tradable_futures.py（2026-09-09 改名）：项目里从来没有 tradable 表，
合约数据只有 futures_base 一张表，旧名字容易误导，故与表名对齐。

抓取逻辑收拢在 app/services/futures_catalog_service.py 的 refresh_contracts()。
本脚本只负责命令行入口 + 数据库会话 + 输出格式，供 cron 每日调用。

品种范围来自本地码表 config/futures_varieties.json（只刷这里列出的品种）：
文件存在就用它，不存在才回退东财品种表（全部品种）。
两种模式都不删除库里已有的历史合约，只做幂等 upsert。

数据来自东方财富：一个交易所一次请求即可拿到该市场全部挂牌合约
（futsseapi.eastmoney.com/list/{市场码}），一轮刷新约 5~6 次请求。

refresh_contracts 语义（只加不删，无 is_active）：
  ① 当前挂牌（东财）、futures_base 里还没有的合约补进去（幂等 upsert，
     同时刷新名称/乘数字段）；
  ② 已入库的合约（含到期后不再挂牌的）一律保留、不改状态。
是否「当前可交易」由查询方按 symbol 交割年月判断（交割月 >= 当前月），
因此本脚本不需要维护 is_active、也不做下架扫描。

用法（在项目根目录）：
    venv/bin/python scripts/refresh_futures_base.py            # 拉取 + upsert（人类可读输出）
    venv/bin/python scripts/refresh_futures_base.py --json     # 只输出 JSON 结果（机器可读）
    venv/bin/python scripts/refresh_futures_base.py --dry-run  # 只看不写
    venv/bin/python scripts/refresh_futures_base.py --underlyings RB,SA   # 临时只刷这两个品种
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根目录（app 包所在）

from app.core.database import Base, engine, SessionLocal
from app.services.futures_catalog_service import VARIETIES_FILE, refresh_contracts

engine.echo = False  # 关闭脚本进程内 SQL 日志，避免刷屏（不影响 app）


def main():
    parser = argparse.ArgumentParser(description='刷新国内期货真实合约字典')
    parser.add_argument('--dry-run', action='store_true', help='只看不写')
    parser.add_argument('--json', action='store_true', help='只输出 JSON 结果（不打印过程行）')
    parser.add_argument('--underlyings', metavar='RB,SA',
                        help='临时只刷这些品种（逗号分隔），不改动 '
                             'config/futures_varieties.json')
    args = parser.parse_args()

    underlyings = ([u for u in args.underlyings.split(',') if u.strip()]
                   if args.underlyings else None)

    if not args.dry_run:
        Base.metadata.create_all(bind=engine)

    if not args.json:
        print(f"品种码表：{VARIETIES_FILE}（{'存在' if VARIETIES_FILE.exists() else '不存在，回退远程品种表'}）")

    db = SessionLocal()
    try:
        result = refresh_contracts(db, dry_run=args.dry_run,
                                   log=None if args.json else print,
                                   underlyings=underlyings)
    finally:
        db.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
