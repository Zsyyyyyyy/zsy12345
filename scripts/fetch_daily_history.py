#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_daily_history.py —— 批量拉取期货历史日K行情，写入 futures_daily_bars

对关注品种清单（DEFAULT_UNDERLYINGS，见下）逐月拼合约 symbol（如 RB2001），
问东方财富日K接口，解析 OHLCV，按 (symbol, trade_date) 幂等 upsert 到 futures_daily_bars。
解析/入库复用 app/services/futures_history_service.py，
与 /api/futures/hist-position 的按需回填共用同一份逻辑（字段/口径一致）。

用法（在项目根目录）：
    venv/bin/python scripts/fetch_daily_history.py                      # 内置清单，2020-01 至今
    venv/bin/python scripts/fetch_daily_history.py --since 2019-01      # 拉到东财保留最早（约 2019）
    venv/bin/python scripts/fetch_daily_history.py --underlyings RB,CU  # 指定品种
    venv/bin/python scripts/fetch_daily_history.py --all                # futures_base 全部品种
    venv/bin/python scripts/fetch_daily_history.py --limit 3            # 试跑前 3 个品种
    venv/bin/python scripts/fetch_daily_history.py --force              # 已入库合约也重拉覆盖
    venv/bin/python scripts/fetch_daily_history.py --dry-run            # 只看规模，不写库
    venv/bin/python scripts/fetch_daily_history.py --json               # 只输出 JSON 结果

说明：
  - 幂等：按 (symbol, trade_date) 唯一键 upsert，可反复跑 / 断点续跑；
  - 默认跳过 futures_daily_bars 里已有数据的合约（增量补），--force 强制全量重拉；
  - 全量约 4200 合约 × ~200 行 ≈ 85 万行，默认并发下约 15~30 分钟，建议先 --dry-run。
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根目录（app 包所在）

from sqlalchemy import select

from app.core.database import Base, engine, SessionLocal
from app.models import FuturesDailyBar
from app.clients.eastmoney_history_client import get_daily_kline
from app.services.futures_history_service import parse_kline_rows, upsert_daily_bars

engine.echo = False  # 只在脚本进程内关闭 SQL 日志，避免逐合约刷屏（不影响 app）

# ---- 关注品种清单（默认拉取目标，按交易所分组，中文名仅作注释）----
# 「空月」两种含义：① 品种尚未上市（上市前的月份，如 AO/BR/LU/LH/BZ/SH/PX/PK/PF/SI/LC/PS/PL）；
#                 ② 单数月合约品种（M/Y/A/C/CS/CF/SR/OI/AP 等偶数月无合约）。
# 只要合约真实挂牌过，东财都能拉到日K（2020 年之后全覆盖）。
DEFAULT_UNDERLYINGS: list[str] = [
    # ---- SHFE 上期所（含 INE 能源）----
    'RB',   # 螺纹钢
    'HC',   # 热轧卷板
    'AL',   # 铝
    'RU',   # 橡胶
    'BU',   # 沥青
    'FU',   # 燃料油
    'SP',   # 纸浆
    'SS',   # 不锈钢
    'AO',   # 氧化铝
    'NR',   # 20号胶
    'LU',   # 低硫燃料油
    'BR',   # 丁二烯橡胶
    'SC',   # 原油（INE）
    # ---- DCE 大商所 ----
    'M',    # 豆粕
    'A',    # 豆一
    'B',    # 豆二
    'Y',    # 豆油
    'P',    # 棕榈油
    'C',    # 玉米
    'CS',   # 玉米淀粉
    'JD',   # 鸡蛋
    'LH',   # 生猪
    'I',    # 铁矿石
    'J',    # 焦炭
    'JM',   # 焦煤
    'L',    # 塑料（LLDPE）
    'PP',   # 聚丙烯
    'V',    # PVC
    'EG',   # 乙二醇
    'EB',   # 苯乙烯
    'PG',   # LPG
    'BZ',   # 纯苯
    # ---- CZCE 郑商所 ----
    'CF',   # 棉花
    'AP',   # 苹果
    'CJ',   # 红枣
    'RM',   # 菜粕
    'OI',   # 菜油
    'TA',   # PTA
    'MA',   # 甲醇
    'UR',   # 尿素
    'SA',   # 纯碱
    'SR',   # 白糖
    'SF',   # 硅铁
    'SM',   # 锰硅
    'FG',   # 玻璃
    'SH',   # 烧碱
    'PF',   # 短纤
    'PK',   # 花生
    'PX',   # 二甲苯
    'PL',   # 丙烯
    # ---- GFEX 广期所 ----
    'SI',   # 工业硅
    'LC',   # 碳酸锂
    'PS',   # 多晶硅
]


def iter_months(since: date, until: date):
    """逐月产出 (year, month)，含端点。"""
    y, m = since.year, since.month
    while (y, m) <= (until.year, until.month):
        yield y, m
        m += 1
        if m == 13:
            y += 1
            m = 1


def fetch_underlying(underlying: str, since: date, until: date,
                     dry_run: bool, force: bool) -> dict:
    """拉一个品种 [since, until] 每个月的合约日K并入库。返回该品种统计 dict。"""
    db = SessionLocal()
    try:
        contracts = rows_total = skipped = empty = errors = 0
        err_examples: list[str] = []
        for y, m in iter_months(since, until):
            symbol = f'{underlying}{y % 100:02d}{m:02d}'
            # 增量模式：库里已有该合约日K则跳过（--force 强制重拉）
            if not force:
                exists = db.scalar(
                    select(FuturesDailyBar.id)
                    .where(FuturesDailyBar.symbol == symbol)
                    .limit(1)
                )
                if exists:
                    skipped += 1
                    continue
            try:
                data = get_daily_kline(symbol)
            except Exception as e:
                errors += 1
                if len(err_examples) < 5:
                    err_examples.append(f'{symbol}: {type(e).__name__}: {e}')
                continue
            if not isinstance(data, list) or not data:
                empty += 1
                continue
            rows = parse_kline_rows(symbol, data)
            if not rows:
                empty += 1
                continue
            contracts += 1
            rows_total += upsert_daily_bars(db, rows, dry_run)
        return {
            'underlying': underlying,
            'contracts': contracts, 'rows': rows_total,
            'skipped': skipped, 'empty': empty, 'errors': errors,
            'err_examples': err_examples,
        }
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description='批量拉取期货历史日K，写入 futures_daily_bars')
    parser.add_argument('--since', default='2020-01', help='起始年月 YYYY-MM，默认 2020-01（东财最早约 2019）')
    parser.add_argument('--until', default='', help='截止年月 YYYY-MM，默认=当前月')
    parser.add_argument('--underlyings', default='',
                        help='逗号分隔品种（如 RB,CU）；缺省=内置关注清单')
    parser.add_argument('--all', action='store_true',
                        help='改用 futures_base 表里的全部品种（而非内置清单）')
    parser.add_argument('--limit', type=int, default=0, help='只处理前 N 个品种（试跑用）')
    parser.add_argument('--workers', type=int, default=4, help='并发品种数，默认 4')
    parser.add_argument('--force', action='store_true', help='已入库的合约也重拉覆盖（默认跳过）')
    parser.add_argument('--dry-run', action='store_true', help='只统计规模，不写库')
    parser.add_argument('--json', action='store_true', help='只输出 JSON 结果（不打印过程行）')
    args = parser.parse_args()

    def say(msg: str) -> None:
        if not args.json:
            print(msg, flush=True)

    try:
        sy, sm = (int(x) for x in args.since.split('-'))
        since = date(sy, sm, 1)
    except (ValueError, IndexError):
        print('--since 格式应为 YYYY-MM，如 2020-01')
        sys.exit(2)
    if args.until:
        try:
            uy, um = (int(x) for x in args.until.split('-'))
            until = date(uy, um, 1)
        except (ValueError, IndexError):
            print('--until 格式应为 YYYY-MM')
            sys.exit(2)
    else:
        today = date.today()
        until = date(today.year, today.month, 1)
    if since > until:
        print('--since 不能晚于 --until')
        sys.exit(2)

    if not args.dry_run:
        Base.metadata.create_all(bind=engine)

    # ---- 品种清单 ----
    if args.underlyings:
        underlyings = [u.strip().upper() for u in args.underlyings.split(',') if u.strip()]
    elif args.all:
        db = SessionLocal()
        try:
            from app.models import FuturesBase
            underlyings = [u for (u,) in db.execute(
                select(FuturesBase.underlying).distinct().order_by(FuturesBase.underlying)
            ).all()]
        finally:
            db.close()
    else:
        underlyings = list(DEFAULT_UNDERLYINGS)
    if args.limit:
        underlyings = underlyings[:args.limit]

    if not underlyings:
        say('⚠ 没有可处理的品种')
        sys.exit(1)

    say(f'品种数：{len(underlyings)} | 范围：{since} ~ {until} | '
        f'workers={args.workers} force={args.force} | dry_run={args.dry_run}')
    say('-' * 60)

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(fetch_underlying, u, since, until, args.dry_run, args.force): u
            for u in underlyings
        }
        for fut in as_completed(futures):
            u = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {'underlying': u, 'contracts': 0, 'rows': 0,
                     'skipped': 0, 'empty': 0, 'errors': 1,
                     'err_examples': [f'{type(e).__name__}: {e}']}
            results.append(r)
            say(f'  {r["underlying"]:6} 合约 {r["contracts"]:3} | 日K {r["rows"]:6} 行 '
                f'| 跳过 {r["skipped"]:3} | 空 {r["empty"]:3} | 失败 {r["errors"]}')

    # ---- 汇总 ----
    summary = {
        'ok': True,
        'since': str(since), 'until': str(until),
        'underlyings': len(underlyings),
        'contracts': sum(r['contracts'] for r in results),
        'rows': sum(r['rows'] for r in results),
        'skipped': sum(r['skipped'] for r in results),
        'empty': sum(r['empty'] for r in results),
        'errors': sum(r['errors'] for r in results),
        'error_examples': [e for r in results for e in r['err_examples']][:10],
        'elapsed_sec': round(time.time() - t0, 1),
        'dry_run': args.dry_run,
    }
    say('-' * 60)
    say(f'合约 {summary["contracts"]} 个 → 日K {summary["rows"]} 行 | '
        f'跳过 {summary["skipped"]} | 空 {summary["empty"]} | 失败 {summary["errors"]} | '
        f'耗时 {summary["elapsed_sec"]}s')
    if args.dry_run:
        say('[DRY-RUN] 未写库，可加 --json 拿到同样统计')
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
