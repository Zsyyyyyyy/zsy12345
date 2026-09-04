#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seed_tradable_futures.py —— 国内可交易期货品种字典种子脚本（幂等 upsert）

数据来源：国内 5 大交易所（上期所 SHFE、大商所 DCE、郑商所 CZCE、广期所 GFEX、
上海国际能源 INE）官网品种上市清单（截至 2024-2025 年）。

字段说明：
- code: 品种 underlying（如 RB 螺纹钢）。持仓 code 形如 nf_<code><4位月份>，如 nf_RB2701
- multiplier: 合约乘数（每点价值，元）。新增持仓时若未传 multiplier，自动按品种表补
- delivery_months: 可交割月份，逗号分隔（如 "1,5,10"）。"1-12" 表示全年
- tick_size: 最小变动价位（用于前端做"价格步进"和后端校验手数合理性）

交易所行情前缀（新浪）：
- SHFE / INE -> hq.sinajs.cn/list=nf_<CODE><MONTH>
- DCE / CZCE / GFEX -> 同上，所有国内期货统一 nf_ 前缀

用法：
    venv/bin/python seed_tradable_futures.py            # 跑 upsert（已存在则更新元数据）
    venv/bin/python seed_tradable_futures.py --dry-run  # 只看不写
    venv/bin/python seed_tradable_futures.py --deactivate RB  # 把某品种置 is_active=False（下市）
"""
import argparse
import sys
from pathlib import Path

# 让脚本能直接 python seed_tradable_futures.py 跑
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select as sa_select
from app.core.database import SessionLocal, engine, Base
from app.models import TradableFuture


# ========== 国内期货品种字典（按交易所分组） ==========
# 字段：(code, 中文名, exchange, multiplier, tick_size, delivery_months, note)
SEEDS: list[tuple[str, str, str, float, float, str, str | None]] = [
    # ---- SHFE 上期所 ----
    ("RB", "螺纹钢", "SHFE", 10, 1, "1,5,10", None),
    ("HC", "热卷", "SHFE", 10, 1, "1,5,10", None),
    ("CU", "铜", "SHFE", 5, 10, "1-12", None),
    ("AL", "铝", "SHFE", 5, 5, "1-12", None),
    ("ZN", "锌", "SHFE", 5, 5, "1-12", None),
    ("PB", "铅", "SHFE", 5, 5, "1-12", None),
    ("SN", "锡", "SHFE", 1, 10, "1-12", None),
    ("NI", "镍", "SHFE", 1, 10, "1-12", None),
    ("AU", "黄金", "SHFE", 1000, 20, "6,12", None),
    ("AG", "白银", "SHFE", 15, 1, "6,12", None),
    ("RU", "橡胶", "SHFE", 10, 5, "1,3,4,5,6,7,8,9,10,11", None),
    ("BU", "沥青", "SHFE", 10, 2, "1-12", None),
    ("FU", "燃料油", "SHFE", 10, 1, "1-12", None),
    ("SP", "纸浆", "SHFE", 10, 2, "1-12", None),
    ("SS", "不锈钢", "SHFE", 5, 5, "1-12", None),
    ("AO", "氧化铝", "SHFE", 20, 1, "1-12", "2023-08-10 上市"),
    # ---- DCE 大商所 ----
    ("M", "豆粕", "DCE", 10, 1, "1,3,5,7,8,9,11,12", None),
    ("Y", "豆油", "DCE", 10, 2, "1,3,5,7,8,9,11,12", None),
    ("P", "棕榈油", "DCE", 10, 2, "1-12", None),
    ("C", "玉米", "DCE", 10, 1, "1,3,5,7,9,11", None),
    ("CS", "玉米淀粉", "DCE", 10, 1, "1,3,5,7,9,11", None),
    ("JD", "鸡蛋", "DCE", 10, 1, "1,3,5,7,9,10,11,12", None),
    ("LH", "生猪", "DCE", 16, 5, "1,3,5,7,9,11", "2021-01-08 上市"),
    ("I", "铁矿石", "DCE", 100, 5, "1-12", None),
    ("J", "焦炭", "DCE", 100, 5, "1-12", None),
    ("JM", "焦煤", "DCE", 60, 5, "1-12", None),
    ("L", "塑料", "DCE", 5, 5, "1-12", None),
    ("PP", "聚丙烯", "DCE", 5, 5, "1-12", None),
    ("PVC", "聚氯乙烯", "DCE", 5, 5, "1-12", None),
    ("EG", "乙二醇", "DCE", 10, 1, "1-12", "2018-12-10 上市"),
    ("EB", "苯乙烯", "DCE", 5, 1, "1-12", "2019-09-26 上市"),
    ("PG", "LPG", "DCE", 20, 1, "1-12", "2020-03-30 上市"),
    ("RR", "粳米", "DCE", 10, 1, "1,3,5,7,9,11", "2019-08-16 上市"),
    # ---- CZCE 郑商所 ----
    ("CF", "棉花", "CZCE", 5, 5, "1,3,5,7,9,11", None),
    ("CY", "棉纱", "CZCE", 5, 5, "1,3,5,7,9,11", "2017-08-18 上市"),
    ("AP", "苹果", "CZCE", 10, 1, "1,3,5,7,10,11,12", "2017-12-22 上市"),
    ("CJ", "焦煤", "CZCE", 50, 5, "1,3,5,7,9,11", None),
    ("RM", "菜粕", "CZCE", 10, 1, "1,3,5,7,8,9,11", None),
    ("OI", "菜油", "CZCE", 10, 2, "1,3,5,7,9,11", None),
    ("TA", "PTA", "CZCE", 5, 2, "1,3,5,7,8,9,11", None),
    ("MA", "甲醇", "CZCE", 10, 1, "1,3,5,7,9,11,12", None),
    ("UR", "尿素", "CZCE", 20, 1, "1,3,5,7,9,11,12", "2019-08-09 上市"),
    ("SA", "纯碱", "CZCE", 20, 1, "1,3,5,7,9,11,12", "2019-12-06 上市"),
    ("SR", "白糖", "CZCE", 10, 1, "1,3,5,7,9,11", None),
    ("SF", "硅铁", "CZCE", 5, 2, "1,2,3,4,5,6,7,8,9,10,11,12", None),
    ("SM", "锰硅", "CZCE", 5, 2, "1,2,3,4,5,6,7,8,9,10,11,12", None),
    ("FG", "玻璃", "CZCE", 20, 1, "1,3,5,7,8,9,11,12", None),
    ("SH", "烧碱", "CZCE", 30, 1, "1,5,9", "2023-09-15 上市"),
    # ---- GFEX 广期所 ----
    ("SI", "工业硅", "GFEX", 5, 5, "1,3,5,7,9,11", "2022-12-22 上市"),
    ("LC", "碳酸锂", "GFEX", 1, 50, "1,3,5,7,9,11", "2023-07-21 上市"),
    # ---- INE 上海国际能源交易中心 ----
    ("SC", "原油", "INE", 1000, 10, "1-12", None),
    ("LU", "低硫燃料油", "INE", 10, 1, "1-12", "2020-06-22 上市"),
    ("BC", "国际铜", "INE", 5, 10, "1-12", "2020-11-19 上市"),
    ("NR", "20号胶", "INE", 10, 5, "1,3,4,5,6,7,8,9,10,11", "2019-08-12 上市"),
    ("EC", "集运指数", "INE", 50, 1, "2,4,6,8,10,12", "2023-08-18 上市"),
]


def parse_months(spec: str) -> list[int]:
    """'1,5,10' / '1-12' / '1,3,5,7,9,11' -> [1, 5, 10, ...]"""
    out: list[int] = []
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-', 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def upsert(db, code: str, name: str, exchange: str, multiplier: float,
           tick_size: float, delivery_months: str, note: str | None, dry_run: bool) -> str:
    """插入或更新一个品种。返回 'inserted' / 'updated' / 'unchanged'。"""
    existing = db.scalar(sa_select(TradableFuture).where(TradableFuture.code == code))
    if existing is None:
        if not dry_run:
            db.add(TradableFuture(
                code=code, name=name, exchange=exchange,
                multiplier=multiplier, tick_size=tick_size,
                delivery_months=delivery_months, note=note, is_active=True,
            ))
        return 'inserted'
    # 已存在：更新元数据（is_active 不动，避免误把下市的又置回来）
    changed = False
    for attr, val in [
        ('name', name), ('exchange', exchange), ('multiplier', multiplier),
        ('tick_size', tick_size), ('delivery_months', delivery_months),
    ]:
        if getattr(existing, attr) != val:
            if not dry_run:
                setattr(existing, attr, val)
            changed = True
    return 'updated' if changed else 'unchanged'


def main():
    parser = argparse.ArgumentParser(description='国内期货品种字典种子脚本')
    parser.add_argument('--dry-run', action='store_true', help='只看不写')
    parser.add_argument('--deactivate', metavar='CODE', help='把某品种置 is_active=False（下市）')
    args = parser.parse_args()

    # 先建表（idempotent）
    if not args.dry_run:
        Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        if args.deactivate:
            tf = db.scalar(sa_select(TradableFuture).where(TradableFuture.code == args.deactivate))
            if tf is None:
                print(f'❌ 品种 {args.deactivate} 不存在')
                sys.exit(1)
            if args.dry_run:
                print(f'  [DRY] 将把 {args.deactivate} 置 is_active=False')
            else:
                tf.is_active = False
                db.commit()
                print(f'✅ 已停用 {args.deactivate}')
            return

        print(f'共 {len(SEEDS)} 条国内期货品种' + ('（DRY-RUN）' if args.dry_run else ''))
        inserted = updated = unchanged = 0
        for code, name, exch, mul, tick, months, note in SEEDS:
            action = upsert(db, code, name, exch, mul, tick, months, note, args.dry_run)
            mark = {'inserted': '+', 'updated': '~', 'unchanged': '='}[action]
            print(f'  {mark} {code:4} {name:8} {exch:5} mul={mul:<6} tick={tick:<4} months={months}')
            if action == 'inserted':
                inserted += 1
            elif action == 'updated':
                updated += 1
            else:
                unchanged += 1

        if not args.dry_run:
            db.commit()
            print(f'✅ 完成：新增 {inserted} / 更新 {updated} / 未变 {unchanged}')
        else:
            print(f'[DRY] 新增 {inserted} / 更新 {updated} / 未变 {unchanged}（未提交）')

        # 校验：解析 delivery_months 不抛错
        bad = []
        for code, _, _, _, _, months, _ in SEEDS:
            try:
                parse_months(months)
            except Exception as e:
                bad.append((code, months, str(e)))
        if bad:
            print('⚠️  delivery_months 解析失败：', bad)
            sys.exit(2)
    finally:
        db.close()


if __name__ == '__main__':
    main()