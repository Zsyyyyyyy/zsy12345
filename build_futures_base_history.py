#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_futures_base_history.py —— 手动补录 futures_base「历史退市合约」

背景：新浪只保留近约 5~7 年（约 2019~2020 至今）已退市合约的日K，
更早的合约新浪已不提供（探测返回 null）。本脚本对每个品种，从 --since 起
逐月拼 symbol（如 RB2001）探测新浪日K，有数据的月份才认为真实存在过，
补进 futures_base 并置 is_active=0；探测为空的月份说明该月无此合约（跳过）。

与每日刷新（refresh_tradable_futures.py / POST /api/futures/refresh-contracts）分工：
  - 刷新接口：只负责 ① 加新挂牌合约 ② 把本次成功抓取交易所里没再出现的在市合约置 0；
  - 本脚本：一次性/按需手动补更早年份的退市合约（近 5~7 年范围）。

前提：先跑过一次刷新（futures_base 里已有各品种在市合约，
      用于继承品种中文名/交易所/乘数字典）。已存在的合约自动跳过、不改 is_active。

用法（在项目根目录）：
    venv/bin/python build_futures_base_history.py                 # 全品种，2019-01 至今
    venv/bin/python build_futures_base_history.py --since 2021-01  # 只补 2021 年以来
    venv/bin/python build_futures_base_history.py --underlyings RB,CU
    venv/bin/python build_futures_base_history.py --limit 3        # 先试前 3 个品种
    venv/bin/python build_futures_base_history.py --dry-run        # 只看要补多少，不写库
    venv/bin/python build_futures_base_history.py --json           # 只输出 JSON 结果
    venv/bin/python build_futures_base_history.py --workers 1 --sleep 0.3  # 保守限速

说明：
  - 幂等可反复跑：断网/失败/新浪临时封禁的月份，下次重跑补上；
  - 新浪探测约 8300 次（90 品种 × ~92 月），默认并发下约 10~20 分钟，
    建议先 --dry-run 看规模再实跑；跑大范围时放后台/nohup。
"""
import argparse
import json
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select

from app.core.database import Base, engine, SessionLocal
from app.models import FuturesBase
from app.routers.data import KLINE_URL, MULTIPLIERS, http_get, parse_jsonp

# 连续合约/具体合约之外的 symbol 判定（探测用）：字母 + 4 位年月
def probe_has_kline(symbol: str) -> bool:
    """探测新浪日K：有真实交易日数据返回 True；null/空/无 d 字段返回 False。"""
    text = http_get(KLINE_URL.format(symbol=urllib.parse.quote(symbol)), enc='utf-8')
    data = parse_jsonp(text)
    if not isinstance(data, list) or not data:
        return False
    for row in data:
        if isinstance(row, dict) and (row.get('d') or '').strip():
            return True
    return False


def iter_months(since: date, until: date):
    """逐月产出 (year, month)，含端点。"""
    y, m = since.year, since.month
    while (y, m) <= (until.year, until.month):
        yield y, m
        m += 1
        if m == 13:
            y += 1
            m = 1


def scan_underlying(underlying: str, since: date, until: date,
                    sleep_s: float, dry_run: bool) -> dict:
    """扫一个品种 [since, until] 的每个月。返回该品种统计 dict。"""
    db = SessionLocal()
    try:
        meta = db.scalar(
            select(FuturesBase)
            .where(FuturesBase.underlying == underlying)
            .order_by(FuturesBase.symbol.desc())
            .limit(1)
        )
        if meta is None:
            return {'underlying': underlying, 'skipped': True,
                    'reason': 'futures_base 里无该品种记录（请先跑一次刷新）'}

        mult, tick = MULTIPLIERS.get(underlying, (None, None))
        probes = found = added = existed = empty = errors = 0
        err_examples: list[str] = []

        for y, m in iter_months(since, until):
            symbol = f'{underlying}{y % 100:02d}{m:02d}'
            probes += 1
            # 已存在（在市或已退市）直接跳过，不覆盖 is_active
            existing_code = db.scalar(
                select(FuturesBase.code).where(FuturesBase.code == 'nf_' + symbol)
            )
            if existing_code:
                existed += 1
                continue
            try:
                if not probe_has_kline(symbol):
                    empty += 1
                    continue
            except Exception as e:
                errors += 1
                if len(err_examples) < 5:
                    err_examples.append(f'{symbol}: {type(e).__name__}: {e}')
                if errors % 10 == 0:
                    time.sleep(1.0)  # 连续失败时退避
                continue
            found += 1
            if not dry_run:
                db.add(FuturesBase(
                    code='nf_' + symbol,
                    symbol=symbol,
                    name=f'{meta.underlying_name}{y % 100:02d}{m:02d}',
                    underlying=underlying,
                    underlying_name=meta.underlying_name,
                    exchange=meta.exchange,
                    multiplier=mult,
                    tick_size=tick,
                    is_active=False,   # 历史退市合约
                ))
                added += 1
            time.sleep(sleep_s)

        if not dry_run:
            db.commit()
        return {
            'underlying': underlying, 'skipped': False,
            'probes': probes, 'found': found, 'added': added,
            'existed': existed, 'empty': empty, 'errors': errors,
            'err_examples': err_examples,
        }
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description='手动补录 futures_base 历史退市合约（新浪逐月探测）')
    parser.add_argument('--since', default='2019-01', help='起始年月 YYYY-MM，默认 2019-01（新浪保留期约 5~7 年）')
    parser.add_argument('--until', default='', help='截止年月 YYYY-MM，默认=当前月')
    parser.add_argument('--underlyings', default='',
                        help='逗号分隔品种（如 RB,CU）；缺省=futures_base 现有全部品种')
    parser.add_argument('--limit', type=int, default=0, help='只处理前 N 个品种（试跑用，按字母序）')
    parser.add_argument('--workers', type=int, default=2, help='并发品种数，默认 2（保守）')
    parser.add_argument('--sleep', type=float, default=0.2, help='同品种逐月探测间隔秒数，默认 0.2')
    parser.add_argument('--dry-run', action='store_true', help='只探测统计，不写库')
    parser.add_argument('--json', action='store_true', help='只输出 JSON 结果（不打印过程行）')
    args = parser.parse_args()

    def say(msg: str) -> None:
        if not args.json:
            print(msg, flush=True)

    try:
        sy, sm = (int(x) for x in args.since.split('-'))
        since = date(sy, sm, 1)
    except (ValueError, IndexError):
        print('--since 格式应为 YYYY-MM，如 2019-01')
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
    db = SessionLocal()
    try:
        if args.underlyings:
            underlyings = [u.strip().upper() for u in args.underlyings.split(',') if u.strip()]
        else:
            underlyings = [u for (u,) in db.execute(
                select(FuturesBase.underlying).distinct().order_by(FuturesBase.underlying)
            ).all()]
        if args.limit:
            underlyings = underlyings[:args.limit]
    finally:
        db.close()

    if not underlyings:
        say('⚠ futures_base 表为空或没有品种记录，请先运行：'
            'venv/bin/python refresh_tradable_futures.py')
        sys.exit(1)

    say(f'品种数：{len(underlyings)} | 范围：{since} ~ {until} | '
        f'workers={args.workers} sleep={args.sleep}s | dry_run={args.dry_run}')
    say('-' * 60)

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(scan_underlying, u, since, until, args.sleep, args.dry_run): u
            for u in underlyings
        }
        for fut in as_completed(futures):
            u = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {'underlying': u, 'skipped': True, 'reason': f'{type(e).__name__}: {e}'}
            results.append(r)
            if r.get('skipped'):
                say(f'  {u:6} 跳过：{r.get("reason")}')
            else:
                say(f'  {u:6} 探测 {r["probes"]:4} 月 | 有数据 {r["found"]:3} '
                    f'(新增 {r["added"]:3}) | 已存在 {r["existed"]:3} | 空 {r["empty"]:4} '
                    f'| 失败 {r["errors"]}')

    # ---- 汇总 ----
    skipped = [r for r in results if r.get('skipped')]
    ok_list = [r for r in results if not r.get('skipped')]
    summary = {
        'ok': True,
        'since': str(since), 'until': str(until),
        'underlyings': len(underlyings),
        'skipped_underlyings': len(skipped),
        'skipped_reasons': [r.get('reason') for r in skipped][:10],
        'probes': sum(r['probes'] for r in ok_list),
        'found': sum(r['found'] for r in ok_list),
        'added': sum(r['added'] for r in ok_list),
        'existed': sum(r['existed'] for r in ok_list),
        'empty': sum(r['empty'] for r in ok_list),
        'errors': sum(r['errors'] for r in ok_list),
        'error_examples': [e for r in ok_list for e in r['err_examples']][:10],
        'elapsed_sec': round(time.time() - t0, 1),
        'dry_run': args.dry_run,
    }
    say('-' * 60)
    say(f'探测 {summary["probes"]} 月 → 有数据 {summary["found"]}（新增 {summary["added"]}）'
        f'| 已存在 {summary["existed"]} | 空 {summary["empty"]} | 失败 {summary["errors"]} | '
        f'耗时 {summary["elapsed_sec"]}s')
    if args.dry_run:
        say('[DRY-RUN] 未写库，可加 --json 拿到同样统计')
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
