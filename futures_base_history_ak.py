#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
futures_base_history_ak.py —— 用 akshare（交易所官方日数据）补录 futures_base 历史退市合约

新浪只保留近约 5~7 年已退市合约；akshare 可以拉到交易所官方日行情快照，回溯更深：
  - SHFE 上期所：可到 2011 年前后
  - CZCE 郑商所：2013 前后（2019/2020 前是 3 位代码时代，如 CF507=2015-07，自动换算成 4 位 CF1507）
  - CFFEX 中金所：2013 前后
  - INE 上海国际能源：2018 上线以来
  - GFEX 广期所：akshare 该源暂空，且其品种（SI/LC 等）上市晚、新浪探测已能覆盖，默认不跑
  - DCE 大商所：akshare 该源当前是坏的（所有日期 JSONDecodeError），默认不跑；
    铁矿石 I1909 等大商所历史合约请改用 build_futures_base_history.py（新浪逐月探测，约 2019/2020 起）

方法：对每个 (年,月) 挑一个交易日，用 ak.get_futures_daily 拉该日全市场合约快照，
凡在 snapshot 里出现的合约 = 当时真实挂牌交易过 → 补进 futures_base 并置 is_active=0。
一个月只采样一天，覆盖该月所有在挂合约；幂等可重跑（已存在跳过、不改 is_active）。

前提：先跑过一次刷新（refresh_tradable_futures.py 或 POST /api/futures/refresh-contracts），
      futures_base 里已有各品种在市合约（用于继承品种中文名/交易所/乘数）。

用法（在项目根目录，需先安装 akshare）：
    venv/bin/python -m pip install akshare          # 仅本脚本需要，跑 app 不需要
    venv/bin/python futures_base_history_ak.py                   # SHFE+CZCE+CFFEX+INE，2019-01 至今
    venv/bin/python futures_base_history_ak.py --since 2016-01   # 更早（仅这几家交易所可到）
    venv/bin/python futures_base_history_ak.py --markets SHFE,CZCE
    venv/bin/python futures_base_history_ak.py --dry-run --json  # 只看规模
    venv/bin/python futures_base_history_ak.py --limit-months 2  # 试跑最近 2 个月
"""
import argparse
import calendar
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select

from app.core.database import Base, engine, SessionLocal
from app.models import FuturesBase
from app.fetchutils import MULTIPLIERS

# 默认启用的交易所（akshare 可用）；DCE 当前坏、GFEX 源空，默认跳过
DEFAULT_MARKETS = ['SHFE', 'CZCE', 'CFFEX', 'INE']
# akshare 内部函数实现在顶层用 ak.get_futures_daily(market=...) 即可

_SYMBOL_RE = re.compile(r'^([A-Za-z]+)(\d+)$')


def decode_symbol(raw_symbol: str, snapshot_year: int) -> tuple[str, int, int] | None:
    """把一个交易所快照代码解析成 (underlying, year, month)。

    - 4 位代码（SHFE/DCE/CFFEX/新郑商所）：如 AG1910 -> 2019-10
    - 3 位代码（老郑商所，2019/2020 前）：如 CF507 -> 2015-07、AP910 -> 2019-10，
      首位是年份个位，结合快照年份在 [snapshot-1, snapshot+24] 窗口内唯一确定。
    无法解析/明显不合理返回 None。
    """
    m = _SYMBOL_RE.match((raw_symbol or '').strip().upper())
    if not m:
        return None
    underlying = m.group(1)
    digits = m.group(2)
    mm: int | None = None
    year: int | None = None

    if len(digits) == 4:
        yy = int(digits[:2])
        mm = int(digits[2:4])
        year = 2000 + yy
    elif len(digits) == 3:
        year_digit = int(digits[0])
        mm = int(digits[1:3])
        # 在快照年附近找「个位数一致」的年份（老郑商所循环使用单一年份位）
        for off in range(-1, 25):
            cand = snapshot_year + off
            if cand % 10 == year_digit:
                year = cand
                break
    else:
        return None

    if mm is None or year is None or not (1 <= mm <= 12):
        return None
    # 合理性窗口：合约从挂牌(约 1~2 年前)交易到交割月
    if not (snapshot_year - 1 <= year <= snapshot_year + 24):
        return None
    # 排除未来过远的月份（交割在 2 年后不合理）
    if year == snapshot_year + 2 and mm > 12:
        return None
    return underlying, year, mm


def canonical_symbol(underlying: str, year: int, mm: int) -> str:
    """统一成 4 位规范码，如 CF1507 / RB2701。"""
    return f'{underlying}{year % 100:02d}{mm:02d}'


def month_range(since: date, until: date):
    y, m = since.year, since.month
    while (y, m) <= (until.year, until.month):
        yield y, m
        m += 1
        if m == 13:
            y += 1
            m = 1


def sample_day_df(ak, market: str, y: int, m: int):
    """在某年月中找一个交易日拉全市场快照。找不到返回 None。"""
    last = calendar.monthrange(y, m)[1]
    days = [15, 14, 16, 13, 17, 20, 10, 25, 5, last]
    seen = set()
    for day in days:
        if day > last or day in seen:
            continue
        seen.add(day)
        d = date(y, m, day)
        if d.weekday() >= 5:
            continue
        ds = f'{d.year}{d.month:02d}{d.day:02d}'
        try:
            df = ak.get_futures_daily(start_date=ds, end_date=ds, market=market)
        except Exception:
            df = None
        if df is not None and len(df):
            return df, ds
    return None, None


def run(markets, since: date, until: date, dry_run: bool, log=None) -> dict:
    import akshare as ak  # 懒加载：只有真正跑时才 import

    def say(msg: str) -> None:
        if log is not None:
            log(msg)

    db = SessionLocal()
    try:
        # ---- 品种元数据：优先在市行，否则任意行；只保留已知品种 ----
        meta: dict[str, dict] = {}
        for row in db.scalars(select(FuturesBase)).all():
            cur = meta.get(row.underlying)
            if cur is None or (cur.get('active') is False and row.is_active):
                meta[row.underlying] = {
                    'name': row.underlying_name or row.underlying,
                    'exchange': row.exchange,
                    'mult': row.multiplier,
                    'tick': row.tick_size,
                    'active': row.is_active,
                }
        known = set(MULTIPLIERS) | set(meta)

        stats = {mk: {'months_ok': 0, 'symbols': 0, 'empty_months': 0, 'broken': 0}
                 for mk in markets}
        total_found = total_added = total_existed = total_unknown = 0
        t0 = time.time()
        pending: list = []  # 攒着批量提交
        months_done = 0

        for y, m in month_range(since, until):
            months_done += 1
            month_new = 0
            for market in markets:
                try:
                    df, ds = sample_day_df(ak, market, y, m)
                except Exception as e:
                    stats[market]['broken'] += 1
                    say(f'  [{y}-{m:02d} {market}] 接口异常：{type(e).__name__}: {str(e)[:100]}')
                    continue
                if df is None or len(df) == 0:
                    stats[market]['empty_months'] += 1
                    continue
                stats[market]['months_ok'] += 1
                month_codes = set()
                for raw in df['symbol'].astype(str):
                    decoded = decode_symbol(raw, y)
                    if decoded is None:
                        continue
                    underlying, yy, mm = decoded
                    if underlying not in known:
                        total_unknown += 1
                        continue
                    symbol = canonical_symbol(underlying, yy, mm)
                    if symbol != (raw or '').strip().upper():
                        pass  # 3位老码已换算成4位规范码
                    month_codes.add(symbol)
                stats[market]['symbols'] += len(month_codes)

                for symbol in sorted(month_codes):
                    code = 'nf_' + symbol
                    exists = db.scalar(
                        select(FuturesBase.code).where(FuturesBase.code == code)
                    )
                    if exists:
                        total_existed += 1
                        continue
                    md = meta.get(_underlying_of(symbol))
                    total_found += 1
                    if not dry_run:
                        pending.append(FuturesBase(
                            code=code,
                            symbol=symbol,
                            name=f'{md["name"]}{symbol[-4:]}' if md else symbol,
                            underlying=_underlying_of(symbol),
                            underlying_name=md['name'] if md else symbol,
                            exchange=md['exchange'] if md else _exchange_of(market),
                            multiplier=md['mult'] if md else None,
                            tick_size=md['tick'] if md else None,
                            is_active=False,
                        ))
                        month_new += 1
                        total_added += 1
                        if len(pending) >= 100:
                            db.add_all(pending)
                            db.commit()
                            pending = []
            if pending and not dry_run:
                db.add_all(pending)
                db.commit()
                pending = []
            if months_done % 10 == 0 or y == until.year and m == until.month:
                say(f'[{months_done}] 到 {y}-{m:02d}：本月新发现 {total_added}，累计 {total_added} 条（已存在 {total_existed}）')

        if not dry_run and pending:
            db.add_all(pending)
            db.commit()

        return {
            'ok': True, 'since': str(since), 'until': str(until),
            'markets': markets,
            'months': months_done,
            'found': total_found, 'added': total_added,
            'existed': total_existed, 'unknown_symbols': total_unknown,
            'per_market': stats,
            'elapsed_sec': round(time.time() - t0, 1), 'dry_run': dry_run,
        }
    finally:
        db.close()


def _underlying_of(symbol: str) -> str:
    """从规范码反解品种：字母前缀。"""
    m = re.match(r'^([A-Za-z]+)\d{4}$', symbol or '')
    return m.group(1) if m else symbol


def _exchange_of(market: str) -> str:
    # INE 品种在本项目里沿用 SHFE 分组（与刷新逻辑一致）
    return 'SHFE' if market == 'INE' else market


def main():
    parser = argparse.ArgumentParser(
        description='akshare 交易所日数据逐月采样，补录 futures_base 历史退市合约')
    parser.add_argument('--since', default='2019-01', help='起始年月 YYYY-MM，默认 2019-01')
    parser.add_argument('--until', default='', help='截止年月 YYYY-MM，默认=当前月')
    parser.add_argument('--markets', default=','.join(DEFAULT_MARKETS),
                        help='交易所，逗号分隔，默认 ' + ','.join(DEFAULT_MARKETS))
    parser.add_argument('--limit-months', type=int, default=0, help='只处理前 N 个月（试跑用）')
    parser.add_argument('--dry-run', action='store_true', help='只统计不写库')
    parser.add_argument('--json', action='store_true', help='只输出 JSON 结果')
    args = parser.parse_args()

    def say(msg: str) -> None:
        if not args.json:
            print(msg, flush=True)

    markets = [mk.strip().upper() for mk in args.markets.split(',') if mk.strip()]
    if not markets:
        print('--markets 不能为空')
        sys.exit(2)
    for mk in markets:
        if mk == 'DCE':
            say('⚠ DCE 在 akshare 中当前不可用（JSONDecodeError），将跳过但计为 broken')
        if mk == 'GFEX':
            say('⚠ GFEX 在 akshare 中暂无可拉取数据（返回空），将跳过')

    try:
        sy, sm = (int(x) for x in args.since.split('-'))
        since = date(sy, sm, 1)
    except (ValueError, IndexError):
        print('--since 格式应为 YYYY-MM')
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

    if args.limit_months:
        # 截断到前 N 个月
        y, m, cnt = since.year, since.month, 0
        limit_until = since
        for _ in range(args.limit_months - 1):
            m += 1
            if m == 13:
                y += 1
                m = 1
            limit_until = date(y, m, 1)
            if limit_until > until:
                break
        if limit_until > until:
            limit_until = until
        say(f'（--limit-months {args.limit_months}）范围截断为 {since} ~ {limit_until}')
        until = limit_until

    say(f'交易所：{",".join(markets)} | 范围：{since} ~ {until} | dry_run={args.dry_run}')
    say('（每月只采一个交易日快照，akshare 慢属正常，全量约 10~30 分钟）')
    t0 = time.time()
    result = run(markets, since, until, args.dry_run, log=say)
    say('-' * 60)
    say(f"完成：共 {result['months']} 个月 → 新发现 {result['found']} 条"
        f"（新增 {result['added']}）| 已存在 {result['existed']} | "
        f"未知品种 {result['unknown_symbols']} | 耗时 {result['elapsed_sec']}s")
    for mk, st in result['per_market'].items():
        say(f"  {mk}: 快照成功 {st['months_ok']} 月 | 合约符号 {st['symbols']} | "
            f"空月 {st['empty_months']} | 异常 {st['broken']}")
    if args.dry_run:
        say('[DRY-RUN] 未写库')
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
