#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_daily_history.py —— 抓取国内期货「具体合约」日级历史行情入库（幂等、可增量）

数据表：futures_daily_bars（模型见 app/models/models.py 的 FuturesDailyBar）
  每行 = 某具体合约某个交易日的 开/高/低/收/成交量/持仓量/结算价
  contract_month = 合约所属交割月份（RB2701 → 2027-01-01，当月第一天）
  (symbol, trade_date) 唯一，重复抓取按此键 upsert，可放心反复跑。

数据来源（零第三方依赖，仅标准库 + sqlalchemy/pymysql，与项目现有栈一致）：
  新浪期货日K：https://stock2.finance.sina.com.cn/futures/api/jsonp.php/
    var%20t=/InnerFuturesNewService.getDailyKLine?symbol=<symbol>
  返回字段：d=日期 o/h/l/c=开高低收 v=成交量 p=持仓量 s=结算价(早期可能为0)

合约来源：tradable_futures 表的全部具体合约（RB2701 等，含已下架），
  先跑 refresh_tradable_futures.py 刷新清单。
  注意：连续合约（RB0）不在默认清单里；如确需可 --symbols RB0 手动指定。

增量策略：每个 symbol 只 upsert 本地已有最大交易日之后的行（含当日重刷），
725 个合约全量约 10 分钟；之后每天跑一次只补最新。

用法（在 WSL 项目根目录）：
    venv/bin/python fetch_daily_history.py                       # 全部具体合约
    venv/bin/python fetch_daily_history.py --symbols RB2701,CU0  # 指定 symbol
    venv/bin/python fetch_daily_history.py --limit 10            # 先试 10 个
    venv/bin/python fetch_daily_history.py --active-only         # 只抓在市合约
    venv/bin/python fetch_daily_history.py --dry-run             # 只抓不写
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select as sa_select, func as sa_func
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.core.database import SessionLocal, engine, Base
from app.models import TradableFuture, FuturesDailyBar

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
REFERER = 'http://finance.sina.com.cn/'
TIMEOUT = 20
KLINE_URL = ('https://stock2.finance.sina.com.cn/futures/api/jsonp.php/'
             'var%20t=/InnerFuturesNewService.getDailyKLine?symbol={symbol}')
BATCH_SIZE = 500  # 每批 upsert 行数

# 具体合约判定：字母 + 4位年月（RB2701 / IF2609）
_CONTRACT_RE = re.compile(r'^([A-Za-z]+)(\d{4})$')


def contract_month_of(symbol: str) -> dt.date | None:
    """从合约代码解析所属交割月份。RB2701 → 2027-01-01；无法解析返回 None。

    仅支持当前通行的 4 位年月编码；郑商所 2019 年前的 3 位老编码（如 AP901）不适用。
    """
    m = _CONTRACT_RE.match(symbol.upper())
    if not m:
        return None
    yymm = m.group(2)
    year, month = 2000 + int(yymm[:2]), int(yymm[2:])
    if not 1 <= month <= 12:
        return None
    return dt.date(year, month, 1)


def http_get_jsonp(symbol: str) -> list[dict]:
    """拉取新浪日K JSONP 并解析为 dict 列表。失败抛异常。"""
    url = KLINE_URL.format(symbol=urllib.parse.quote(symbol))
    req = urllib.request.Request(url, headers={
        'Referer': REFERER, 'User-Agent': UA, 'Accept': '*/*',
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode('utf-8', errors='replace')

    # 形如 /*<script>...*/\nvar t=([{...},...]); —— 剥注释、var 前缀、外层括号
    m = re.search(r'var\s+\w+\s*=\s*(.*)$', raw.strip(), re.DOTALL)
    if not m:
        raise ValueError('响应不含 var 声明：' + raw[:80])
    payload = m.group(1).strip().rstrip(';').strip()
    if payload.startswith('(') and payload.endswith(')'):
        payload = payload[1:-1]
    data = json.loads(payload)
    return data if isinstance(data, list) else []


def to_float(s) -> float | None:
    try:
        v = float(s)
        return v if v == v else None  # 过滤 NaN
    except (TypeError, ValueError):
        return None


def to_int(s) -> int | None:
    f = to_float(s)
    return None if f is None else int(f)


def parse_rows(symbol: str, data: list[dict]) -> list[dict]:
    """新浪原始行 -> 表行。过滤无日期/无价格的脏行。"""
    month = contract_month_of(symbol)
    rows = []
    for d in data:
        day = (d.get('d') or '').strip()
        try:
            trade_date = dt.date.fromisoformat(day)
        except ValueError:
            continue
        close = to_float(d.get('c'))
        if close is None or close <= 0:
            continue  # 无成交的老合约日K可能是全 0 占位
        rows.append({
            'symbol': symbol,
            'trade_date': trade_date,
            'contract_month': month,
            'open_price': to_float(d.get('o')),
            'high': to_float(d.get('h')),
            'low': to_float(d.get('l')),
            'close': close,
            'volume': to_int(d.get('v')),
            'open_interest': to_int(d.get('p')),
            # 结算价：0 视为缺失（早期数据新浪填 0）
            'settlement': (to_float(d.get('s')) or None),
        })
    return rows


def upsert_rows(db, rows: list[dict], dry_run: bool) -> int:
    """MySQL 批量 upsert（ON DUPLICATE KEY UPDATE）。返回写入行数。"""
    if not rows or dry_run:
        return len(rows) if dry_run else 0
    written = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        stmt = mysql_insert(FuturesDailyBar).values(batch)
        stmt = stmt.on_duplicate_key_update(
            contract_month=stmt.inserted.contract_month,
            open_price=stmt.inserted.open_price,
            high=stmt.inserted.high,
            low=stmt.inserted.low,
            close=stmt.inserted.close,
            volume=stmt.inserted.volume,
            open_interest=stmt.inserted.open_interest,
            settlement=stmt.inserted.settlement,
        )
        db.execute(stmt)
        written += len(batch)
    db.commit()
    return written


def last_date_of(db, symbol: str) -> dt.date | None:
    return db.scalar(
        sa_select(sa_func.max(FuturesDailyBar.trade_date))
        .where(FuturesDailyBar.symbol == symbol)
    )


def fetch_symbol(db, symbol: str, dry_run: bool, full_refresh: bool) -> tuple[int, int, str]:
    """抓一个 symbol。返回 (新增+更新行数, 总行数, 状态)。"""
    raw = http_get_jsonp(symbol)
    rows = parse_rows(symbol, raw)
    if not rows:
        return 0, 0, 'no-data'

    # 增量：只 upsert 本地最大交易日及之后的行（覆盖当日盘中写入的半根K）
    if not full_refresh:
        last = last_date_of(db, symbol)
        if last is not None:
            rows = [r for r in rows if r['trade_date'] >= last]
    written = upsert_rows(db, rows, dry_run)
    return written, len(rows), 'ok'


def main():
    parser = argparse.ArgumentParser(description='抓取国内期货具体合约日级历史行情入库')
    parser.add_argument('--symbols', default='',
                        help='逗号分隔的 symbol 列表（如 RB2701,RB0），缺省=tradable_futures 全部具体合约')
    parser.add_argument('--active-only', action='store_true',
                        help='只抓 is_active=1 的在市合约（默认含已下架合约，历史更全）')
    parser.add_argument('--limit', type=int, default=0, help='只处理前 N 个合约（试跑用）')
    parser.add_argument('--full-refresh', action='store_true',
                        help='忽略增量起点，全量 upsert（表结构变更/数据修复时用）')
    parser.add_argument('--sleep', type=float, default=0.3, help='请求间隔秒数')
    parser.add_argument('--dry-run', action='store_true', help='只抓取解析，不写库')
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)  # 自动建表（已存在则无副作用）

    # ---- 组装 symbol 清单 ----
    db = SessionLocal()
    try:
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
        else:
            q = (sa_select(TradableFuture.symbol, TradableFuture.name, TradableFuture.exchange)
                 .order_by(TradableFuture.symbol))
            if args.active_only:
                q = q.where(TradableFuture.is_active.is_(True))
            contract_rows = db.execute(q).all()
            if not contract_rows:
                print('⚠ tradable_futures 表为空，请先运行：venv/bin/python refresh_tradable_futures.py')
                return
            if args.limit:
                contract_rows = contract_rows[:args.limit]
            symbols = [r.symbol for r in contract_rows]
            print(f'合约数：{len(contract_rows)}')
            for r in contract_rows[:10]:
                print(f'  {r.symbol:10} {r.name} [{r.exchange}]')
            if len(contract_rows) > 10:
                print(f'  ... 等共 {len(contract_rows)} 个')

        total_written = total_rows = ok = no_data = failed = 0
        t0 = time.time()
        for i, symbol in enumerate(symbols, 1):
            try:
                written, nrows, status = fetch_symbol(db, symbol, args.dry_run, args.full_refresh)
                total_written += written
                total_rows += nrows
                if status == 'ok':
                    ok += 1
                else:
                    no_data += 1
                print(f'[{i}/{len(symbols)}] {symbol:10} {status:8} 抓到 {nrows:5} 行，写入 {written:5} 行')
            except Exception as e:
                failed += 1
                print(f'[{i}/{len(symbols)}] {symbol:10} ✗ 失败：{e}')
            time.sleep(args.sleep)
    finally:
        db.close()

    print('-' * 60)
    print(f'✅ 完成：成功 {ok} / 无数据 {no_data} / 失败 {failed}，'
          f'共写入 {total_written} 行，耗时 {time.time() - t0:.1f}s')
    if args.dry_run:
        print('[DRY-RUN] 未写库')


if __name__ == '__main__':
    main()
