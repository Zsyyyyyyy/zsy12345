#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把历史遗留的 tradable 表并入 futures_base（一次性迁移脚本）。

设计原则——宁可少做，不可误伤：
  * 默认 dry-run，只统计不写库；
  * 自动探测表名（SHOW TABLES LIKE '%tradable%'）与列名，不依赖固定结构；
  * 迁移前自动备份为 <原表名>_bak_YYYYMMDD；
  * 只有显式传 --drop 才删原表。

用法（项目根目录）：
    python scripts/migrate_tradable_to_base.py                    # 干跑
    python scripts/migrate_tradable_to_base.py --table tradable   # 指定表名
    python scripts/migrate_tradable_to_base.py --apply            # 真迁（含备份）
    python scripts/migrate_tradable_to_base.py --apply --drop     # 迁完删原表
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.clients.akshare_client import _EXCHANGE_CN2CODE
from app.core.database import SessionLocal, engine
from app.fetchutils import MULTIPLIERS, _CONTRACT_RE
from app.services.futures_catalog_service import _upsert_contract

# 列名候选：不同历史版本的字段命名不一样，逐个试，取第一个非空值
COLUMN_CANDIDATES = {
    "code": ("code", "nf_code", "contract_code", "full_code", "symbol_code"),
    "symbol": ("symbol", "contract", "contract_symbol", "code2"),
    "name": ("name", "contract_name", "display_name", "title"),
    "underlying": ("underlying", "product", "product_code", "variety", "variety_code"),
    "underlying_name": ("underlying_name", "product_name", "variety_name"),
    "exchange": ("exchange", "exch", "market", "exchange_code"),
    "multiplier": ("multiplier", "contract_multiplier", "multi", "per_point"),
    "tick_size": ("tick_size", "tick", "min_price_move", "min_tick"),
}


def pick(row: dict, field: str) -> str:
    """按候选列取值，返回干净字符串（找不到或空值返回 ''）。"""
    for col in COLUMN_CANDIDATES[field]:
        if col in row and row[col] is not None:
            s = str(row[col]).strip()
            if s and s.lower() not in ("none", "nan", "null"):
                return s
    return ""


def to_float(s: str):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def find_tables(conn) -> list[str]:
    names = [r[0] for r in conn.execute(text("SHOW TABLES")).fetchall()]
    return [n for n in names if "tradable" in n.lower()]


def main():
    parser = argparse.ArgumentParser(description="tradable 表并入 futures_base")
    parser.add_argument("--apply", action="store_true", help="真写入（默认干跑）")
    parser.add_argument("--drop", action="store_true", help="迁移后删除原表（需配合 --apply）")
    parser.add_argument("--table", default="", help="指定原表名；不传则自动探测含 tradable 的表")
    args = parser.parse_args()

    dry_run = not args.apply
    db = SessionLocal()
    try:
        with engine.connect() as conn:
            tables = [args.table] if args.table else find_tables(conn)
            if not tables:
                print("未找到任何含 tradable 的表——项目里只有 futures_base，无需迁移。")
                return

            for table in tables:
                cols = [r[0] for r in conn.execute(text(f"SHOW COLUMNS FROM `{table}`")).fetchall()]
                rows = conn.execute(text(f"SELECT * FROM `{table}`")).mappings().all()
                print(f"\n== 表 {table}：{len(rows)} 行，列：{', '.join(cols)}")

                inserted = updated = unchanged = skipped = 0
                for raw in rows:
                    row = dict(raw)
                    symbol = pick(row, "symbol").upper()
                    code = pick(row, "code").lower()
                    if not symbol and code.startswith("nf_"):
                        symbol = code[3:].upper()
                    if not code and symbol:
                        code = "nf_" + symbol
                    match = _CONTRACT_RE.match(symbol)
                    if not match:
                        skipped += 1
                        continue
                    underlying = pick(row, "underlying").upper() or match.group(1)
                    name = pick(row, "name") or f"{pick(row, 'underlying_name')}{match.group(2)}"
                    multiplier = to_float(pick(row, "multiplier"))
                    tick_size = to_float(pick(row, "tick_size"))
                    default_mult, default_tick = MULTIPLIERS.get(underlying, (None, None))
                    # 旧表可能存中文交易所名（"郑州商品交易所"），统一成 CZCE 这类代码
                    exchange = pick(row, "exchange")
                    exchange = _EXCHANGE_CN2CODE.get(exchange, exchange.upper())
                    action = _upsert_contract(
                        db, code, symbol, name or symbol, underlying,
                        pick(row, "underlying_name"), exchange,
                        multiplier if multiplier is not None else default_mult,
                        tick_size if tick_size is not None else default_tick,
                        dry_run,
                    )
                    if action == "inserted":
                        inserted += 1
                    elif action == "updated":
                        updated += 1
                    else:
                        unchanged += 1

                print(f"   新增 {inserted} / 更新 {updated} / 未变 {unchanged} / 跳过(非合约行) {skipped}")

                if dry_run:
                    print("   [DRY-RUN] 未写入，加 --apply 才生效")
                    continue

                backup = f"{table}_bak_{date.today():%Y%m%d}"
                with engine.begin() as w:
                    w.execute(text(f"CREATE TABLE `{backup}` AS SELECT * FROM `{table}`"))
                print(f"   已备份 -> {backup}")

                db.commit()

                if args.drop:
                    with engine.begin() as w:
                        w.execute(text(f"DROP TABLE `{table}`"))
                    print(f"   已删除原表 {table}（备份仍在 {backup}）")
                else:
                    print(f"   原表 {table} 保留未删（需要删除请加 --drop）")

        total = db.scalar(text("SELECT COUNT(*) FROM futures_base"))
        print(f"\nfutures_base 现有 {total} 条")
    finally:
        db.close()


if __name__ == "__main__":
    main()
