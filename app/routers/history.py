#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
history.py —— 历史行情 / 品种信息接口（读数据库），全部返回 JSON

品种/合约库（原 futures_base.py 并入）：
  GET  /api/futures-base                列出合约库（默认在市）
  GET  /api/futures-base/search         搜索在市合约
  GET  /api/futures-base/{code}        查单个合约（含历史退市）
  POST /api/futures-base/validate       批量校验 code
历史行情与定时导入：
  GET  /api/futures/hist-position       当前价在历年同月合约区间位置（读库）
  GET  /api/history/dailybars           从 futures_daily_bars 出历史日K（读库）
  POST /api/futures/refresh-contracts   定时导入：刷新在市合约（后台任务）
  POST /api/futures/fetch-history       定时导入：抓日K入库（后台任务）
  GET  /api/futures/jobs/{job_id}          查询导入任务进度

维护脚本 refresh_tradable_futures.py / fetch_daily_history.py 也调用本文件的
refresh_contracts / run_fetch_history 完成「定时导入」。
"""
import json
import re
import threading
import time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.security import get_current_user
from app.fetchutils import (BATCH_SIZE, CONTRACT_URL, EXCHANGE_MAP, MULTIPLIERS,
                            NODE_LIST_URL, SLEEP, _CONTRACT_RE, http_get, parse_jsonp)
from app.models import FuturesBase, FuturesDailyBar, User
from app.schemas import FuturesBaseOut

router = APIRouter(tags=["history"])

def fetch_nodes() -> list[tuple[str, str, str]]:
    """拉取品种 node 映射。返回 [(中文名, node, exchange), ...]。"""
    js = http_get(NODE_LIST_URL, enc='gb2312')
    out: list[tuple[str, str, str]] = []
    for exch_key in ('czce', 'dce', 'shfe', 'cffex', 'gfex'):
        # 截取该交易所的数组段
        i = js.find(exch_key + ' :')
        if i < 0:
            i = js.find(exch_key + ':')
        if i < 0:
            continue
        seg = js[i:]
        seg = seg[seg.find('['):]
        depth = 0
        end = None
        for idx, ch in enumerate(seg):
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    end = idx
                    break
        arr = seg[:end + 1] if end is not None else seg
        for name, node in re.findall(r"\['([^']+)',\s*'([^']+)'\s*,", arr):
            if node.endswith('_qh'):
                out.append((name, node, EXCHANGE_MAP[exch_key]))
    return out


def fetch_contracts(node: str) -> list[dict]:
    """拉取某品种全部挂牌合约。返回原始 dict 列表。"""
    return json.loads(http_get(CONTRACT_URL.format(node=node)))


def _upsert_contract(db: Session, code: str, symbol: str, name: str, underlying: str,
                     underlying_name: str, exchange: str, multiplier: float | None,
                     tick_size: float | None, dry_run: bool) -> str:
    """插入或更新一个合约。返回 inserted/updated/unchanged。"""
    existing = db.scalar(select(FuturesBase).where(FuturesBase.code == code))
    if existing is None:
        if not dry_run:
            db.add(FuturesBase(
                code=code, symbol=symbol, name=name, underlying=underlying,
                underlying_name=underlying_name, exchange=exchange,
                multiplier=multiplier, tick_size=tick_size, is_active=True,
            ))
        return 'inserted'
    changed = False
    for attr, val in [
        ('symbol', symbol), ('name', name), ('underlying', underlying),
        ('underlying_name', underlying_name), ('exchange', exchange),
        ('multiplier', multiplier), ('tick_size', tick_size),
        ('is_active', True),
    ]:
        if getattr(existing, attr) != val:
            if not dry_run:
                setattr(existing, attr, val)
            changed = True
    return 'updated' if changed else 'unchanged'


def refresh_contracts(db: Session, dry_run: bool = False, log=None) -> dict:
    """刷新 futures_base 在市合约（新浪当前挂牌，幂等 upsert）。

    只做两件事：
      ① 把当前挂牌、表里还没有的新合约补进去（is_active=1）；
      ② 把「本次成功抓取到的交易所」里没再出现的在市合约置 is_active=0
         （到期下架）。某个交易所本次拉取失败时不下架其合约，避免误杀。
    已退市的历史合约（更早年份）不在此维护，由 build_futures_base_history.py 补录。

    log 为可调用对象（接收一行文本）或 None（静默，供 --json / 后台任务用）。
    返回统计 dict（可直接 JSON 序列化）。
    """
    def say(msg: str) -> None:
        if log is not None:
            log(msg)

    nodes = fetch_nodes()
    say(f'品种 node 数：{len(nodes)}')
    seen_codes: set[str] = set()
    scanned_exchanges: set[str] = set()
    inserted = updated = unchanged = skipped = failed = 0

    for cn_name, node, exchange in nodes:
        try:
            contracts = fetch_contracts(node)
        except Exception as e:
            say(f'  ✗ {cn_name:8} node={node:12} 拉取失败：{e}')
            failed += 1
            continue
        scanned_exchanges.add(exchange)

        for c in contracts:
            symbol = (c.get('symbol') or '').upper()
            m = _CONTRACT_RE.match(symbol)
            if not m:
                # 连续合约（RB0）或异常 symbol，跳过
                skipped += 1
                continue
            underlying = m.group(1)
            code = 'nf_' + symbol
            seen_codes.add(code)
            name = c.get('name') or ''
            # 新浪把「10 月合约」（如 AU2610）的 name 误标为「连续」，
            # 修正为「品种中文名 + 年月」
            if name.endswith('连续'):
                name = f'{cn_name}{m.group(2)}'
            mult, tick = MULTIPLIERS.get(underlying, (None, None))
            action = _upsert_contract(db, code, symbol, name, underlying, cn_name,
                                      exchange, mult, tick, dry_run)
            if action == 'inserted':
                inserted += 1
            elif action == 'updated':
                updated += 1
            else:
                unchanged += 1
        time.sleep(SLEEP)

    # 把「本次成功抓取到的交易所」里没再出现的在市合约置 is_active=0（到期下架）
    deactivated = 0
    active: int | None = None
    if not dry_run:
        all_rows = db.scalars(select(FuturesBase)).all()
        for row in all_rows:
            if (row.is_active and row.code not in seen_codes
                    and row.exchange in scanned_exchanges):
                row.is_active = False
                deactivated += 1
        db.commit()
        active = len(db.scalars(
            select(FuturesBase).where(FuturesBase.is_active.is_(True))
        ).all())

    say('-' * 60)
    say(f'新增 {inserted} / 更新 {updated} / 未变 {unchanged} / '
        f'跳过(连续等) {skipped} / 拉取失败品种 {failed} / 下架 {deactivated}')
    if not dry_run:
        say(f'✅ 完成：当前可交易合约 {active} 条')
    else:
        say('[DRY-RUN] 未提交')
    return {
        'dry_run': dry_run, 'nodes': len(nodes),
        'inserted': inserted, 'updated': updated, 'unchanged': unchanged,
        'skipped': skipped, 'failed': failed, 'deactivated': deactivated,
        'active': active,
    }


# =====================================================================
# 四、日级历史行情入库（原 fetch_daily_history.py 的逻辑）
# =====================================================================


KLINE_URL = ('https://stock2.finance.sina.com.cn/futures/api/jsonp.php/'
             'var%20t=/InnerFuturesNewService.getDailyKLine?symbol={symbol}')
BATCH_SIZE = 500  # 每批 upsert 行数



def contract_month_of(symbol: str) -> date | None:
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
    return date(year, month, 1)


def _to_float(s) -> float | None:
    try:
        v = float(s)
        return v if v == v else None  # 过滤 NaN
    except (TypeError, ValueError):
        return None


def _to_int(s) -> int | None:
    f = _to_float(s)
    return None if f is None else int(f)


def _parse_kline_rows(symbol: str, data: list[dict]) -> list[dict]:
    """新浪日K原始行 -> 表行。过滤无日期/无价格的脏行。"""
    month = contract_month_of(symbol)
    rows = []
    for d in data:
        day = (d.get('d') or '').strip()
        try:
            trade_date = date.fromisoformat(day)
        except ValueError:
            continue
        close = _to_float(d.get('c'))
        if close is None or close <= 0:
            continue  # 无成交的老合约日K可能是全 0 占位
        rows.append({
            'symbol': symbol,
            'trade_date': trade_date,
            'contract_month': month,
            'open_price': _to_float(d.get('o')),
            'high': _to_float(d.get('h')),
            'low': _to_float(d.get('l')),
            'close': close,
            'volume': _to_int(d.get('v')),
            'open_interest': _to_int(d.get('p')),
            # 结算价：0 视为缺失（早期数据新浪填 0）
            'settlement': (_to_float(d.get('s')) or None),
        })
    return rows


def _upsert_daily_bars(db: Session, rows: list[dict], dry_run: bool) -> int:
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


def _last_date_of(db: Session, symbol: str) -> date | None:
    return db.scalar(
        select(func.max(FuturesDailyBar.trade_date))
        .where(FuturesDailyBar.symbol == symbol)
    )


def _fetch_symbol(db: Session, symbol: str, dry_run: bool, full_refresh: bool) -> tuple[int, int, str]:
    """抓一个 symbol 的日K并入库。返回 (新增+更新行数, 总行数, 状态)。"""
    raw = http_get(KLINE_URL.format(symbol=urllib.parse.quote(symbol)), enc='utf-8')
    data = parse_jsonp(raw)
    if not isinstance(data, list):
        return 0, 0, 'no-data'
    rows = _parse_kline_rows(symbol, data)
    if not rows:
        return 0, 0, 'no-data'

    # 增量：只 upsert 本地最大交易日及之后的行（覆盖当日盘中写入的半根K）
    if not full_refresh:
        last = _last_date_of(db, symbol)
        if last is not None:
            rows = [r for r in rows if r['trade_date'] >= last]
    written = _upsert_daily_bars(db, rows, dry_run)
    return written, len(rows), 'ok'


def run_fetch_history(db: Session, symbols: list[str] | None = None, active_only: bool = True,
                      limit: int = 0, full_refresh: bool = False, sleep_s: float = 0.3,
                      dry_run: bool = False, log=None) -> dict:
    """抓取国内期货具体合约日级历史行情入库（幂等、可增量）。

    symbols 为 None 时默认取 futures_base 表全部具体合约（含已下架）。
    log 为可调用对象或 None（静默，供 --json / 后台任务用）。返回统计 dict。
    """
    def say(msg: str) -> None:
        if log is not None:
            log(msg)

    # ---- 组装 symbol 清单 ----
    if symbols is None:
        q = (select(FuturesBase.symbol, FuturesBase.name, FuturesBase.exchange)
             .order_by(FuturesBase.symbol))
        if active_only:
            q = q.where(FuturesBase.is_active.is_(True))
        contract_rows = db.execute(q).all()
        if not contract_rows:
            msg = ('⚠ futures_base 表为空，请先运行：'
                   'venv/bin/python refresh_tradable_futures.py')
            say(msg)
            return {'ok': False, 'reason': 'futures_base 表为空，请先刷新合约清单', 'written': 0}
        if limit:
            contract_rows = contract_rows[:limit]
        symbols = [r.symbol for r in contract_rows]
        say(f'合约数：{len(contract_rows)}')
        for r in contract_rows[:10]:
            say(f'  {r.symbol:10} {r.name} [{r.exchange}]')
        if len(contract_rows) > 10:
            say(f'  ... 等共 {len(contract_rows)} 个')

    total_written = total_rows = ok = no_data = failed = 0
    t0 = time.time()
    for i, symbol in enumerate(symbols, 1):
        try:
            written, nrows, status = _fetch_symbol(db, symbol, dry_run, full_refresh)
            total_written += written
            total_rows += nrows
            if status == 'ok':
                ok += 1
            else:
                no_data += 1
            say(f'[{i}/{len(symbols)}] {symbol:10} {status:8} 抓到 {nrows:5} 行，写入 {written:5} 行')
        except Exception as e:
            failed += 1
            say(f'[{i}/{len(symbols)}] {symbol:10} ✗ 失败：{e}')
        time.sleep(sleep_s)

    say('-' * 60)
    say(f'✅ 完成：成功 {ok} / 无数据 {no_data} / 失败 {failed}，'
        f'共写入 {total_written} 行，耗时 {time.time() - t0:.1f}s')
    if dry_run:
        say('[DRY-RUN] 未写库')
    return {
        'ok': True, 'symbols': len(symbols), 'succeeded': ok, 'no_data': no_data,
        'failed': failed, 'total_rows': total_rows, 'total_written': total_written,
        'elapsed_sec': round(time.time() - t0, 1), 'dry_run': dry_run,
    }


# =====================================================================
# 五、后台任务（耗时抓取通过 POST 开任务立即返回 job_id，进度轮询）
# =====================================================================


JOBS: dict[str, dict] = {}
_BJ_TZ = timezone(timedelta(hours=8))
_JOB_LOG_CAP = 300  # 每个任务保留最近多少行日志（防内存膨胀）


def _now_bj() -> str:
    return datetime.now(_BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')


def _append_job_log(record: dict, msg: str) -> None:
    record['lines'].append(msg)
    if len(record['lines']) > _JOB_LOG_CAP:
        record['lines'] = record['lines'][-_JOB_LOG_CAP:]


def _run_job(job_id: str) -> None:
    """后台线程：执行任务并回写结果。注意用独立 Session，勿复用请求的 db。"""
    record = JOBS.get(job_id)
    db = SessionLocal()
    try:
        kind, params = record['kind'], record['params']
        log = lambda m: _append_job_log(record, m)  # noqa: E731
        if kind == 'refresh_contracts':
            result = refresh_contracts(db, dry_run=params.get('dry_run', False), log=log)
        elif kind == 'fetch_history':
            result = run_fetch_history(
                db,
                symbols=params.get('symbols'),
                active_only=params.get('active_only', True),
                limit=params.get('limit', 0),
                full_refresh=params.get('full_refresh', False),
                sleep_s=params.get('sleep', SLEEP),
                dry_run=params.get('dry_run', False),
                log=log,
            )
        else:
            raise ValueError('未知任务类型: ' + str(kind))
        record['result'] = result
        record['status'] = 'done'
    except Exception as e:
        record['status'] = 'error'
        record['error'] = f'{type(e).__name__}: {e}'
    finally:
        record['finished_at'] = _now_bj()
        db.close()


def _start_job(kind: str, params: dict) -> str:
    """登记并启动后台任务，返回 job_id。"""
    job_id = uuid.uuid4().hex[:12]
    record = {
        'job_id': job_id,
        'kind': kind,
        'status': 'running',
        'created_at': _now_bj(),
        'finished_at': None,
        'lines': [],
        'result': None,
        'error': None,
        'params': params,
    }
    JOBS[job_id] = record
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return job_id


# =====================================================================
# 六、HTTP 端点
# =====================================================================

# ---------- 实时行情 / 联想 / 分时 / 日K（公开，纯代理转发，输出 JSON） ----------


# ---------- 历史价格位置：历年同月合约合并区间（近 7 年，2019+） ----------

# 示例：持仓 nf_RB2701（2027-01 交割），想知现价在历年 1 月合约（RB2001…RB2601）
# 合并价格区间里的位置。旧年份（<2019）新浪无数据，返回里用 skipped_years 标注。
_CODE4_RE = re.compile(r'^nf_([A-Za-z]+)(\d{4})$')
_HIST_SINCE = 2019  # 新浪对具体合约保留约 5~7 年


def _parse_contract_code(code: str):
    """nf_RB2701 -> (underlying='RB', 交割年=2027, 月=1)。非法返回 None。"""
    raw = (code or '').strip().lower()  # 统一小写以匹配 nf_ 前缀，字母部分随后转大写
    m = _CODE4_RE.match(raw)
    if not m:
        return None
    underlying = m.group(1).upper()
    d4 = m.group(2)
    yy, mm = int(d4[:2]), int(d4[2:])
    if not 1 <= mm <= 12:
        return None
    return underlying, 2000 + yy, mm


def _try_sina_daily_rows(canonical_symbol: str, quote_symbol: str) -> list[dict] | None:
    """用 quote_symbol 问新浪日K，按 canonical_symbol 解析成表行；无数据返回 None。"""
    text = http_get(KLINE_URL.format(symbol=urllib.parse.quote(quote_symbol)), enc='utf-8')
    data = parse_jsonp(text)
    if not isinstance(data, list) or not data:
        return None
    rows = _parse_kline_rows(canonical_symbol, data)
    return rows or None


def _daily_rows_of_contract(db: Session, underlying: str, year: int, mm: int):
    """取某 (品种, 交割年月) 合约的日K行（按日期升序）。库里没有则现抓新浪并入库。"""
    sym = f'{underlying}{year % 100:02d}{mm:02d}'
    stmt = (select(FuturesDailyBar)
            .where(FuturesDailyBar.symbol == sym)
            .order_by(FuturesDailyBar.trade_date))
    rows = db.scalars(stmt).all()
    if rows:
        return rows
    # 尝试的查询代码：4 位规范码；郑商所 2021 年前可能只有 3 位老码（如 TA901）
    attempts = [sym]
    if year < 2021:
        attempts.append(f'{underlying}{year % 10}{mm:02d}')
    for quote in attempts:
        parsed = _try_sina_daily_rows(sym, quote)
        if not parsed:
            continue
        _upsert_daily_bars(db, parsed, dry_run=False)
        db.commit()
        return db.scalars(stmt).all()
    return []


@router.get('/api/futures/hist-position')
def futures_hist_position(code: str = '', price: float | None = None,
                          db: Session = Depends(get_db)):
    """当前价在「历年同月合约」合并历史区间里的位置。

    /api/futures/hist-position?code=nf_RB2701&price=3173

    逻辑：取 2019 年以来每年「同交割月」合约（RB2001…RB2601）的全部日收盘价做池，
    算 price 落在池中的百分位与区间。数据源：futures_daily_bars 表（没有的合约
    现抓新浪日K并入库，之后秒回）。更早年（<2019）新浪无数据，会列出 skipped_years。

    返回：
      {code, underlying, delivery_month, price, price_from,
       stats:{days,min,max,avg,median,pct,between...},
       per_year:[{year,symbol,days,min,max}...], skipped_years:[...]}
    """
    parsed = _parse_contract_code(code)
    if parsed is None:
        raise HTTPException(status_code=400, detail='code 需形如 nf_RB2701（4位年月）')
    underlying, cur_year, mm = parsed
    if cur_year > 9999 or cur_year < 2000:
        raise HTTPException(status_code=400, detail='code 年份不合法')

    # price 缺省时用该合约自身日K最后一根收盘价
    price_from = 'param'
    if price is None:
        own = _daily_rows_of_contract(db, underlying, cur_year, mm)
        if not own:
            raise HTTPException(status_code=404,
                                detail=f'{code} 暂无历史数据（可能新浪未收录）')
        price = float(own[-1].close)
        price_from = 'self-last-close'

    per_year = []
    skipped_years: list[str] = []
    pool: list[float] = []
    for y in range(_HIST_SINCE, cur_year):
        rows = _daily_rows_of_contract(db, underlying, y, mm)
        if not rows:
            skipped_years.append(f'{y}年{mm:02d}月（新浪无数据）')
            continue
        closes = [float(r.close) for r in rows if r.close is not None]
        if not closes:
            skipped_years.append(f'{y}年{mm:02d}月（无有效收盘）')
            continue
        pool.extend(closes)
        per_year.append({
            'year': y,
            'symbol': f'{underlying}{y % 100:02d}{mm:02d}',
            'days': len(closes),
            'min': min(closes),
            'max': max(closes),
            'avg': round(sum(closes) / len(closes), 2),
        })

    if not pool:
        return {
            'ok': False, 'code': code, 'underlying': underlying,
            'delivery_month': f'{cur_year}-{mm:02d}', 'price': price,
            'reason': '近 7 年无同月历史合约数据（新浪未收录更早）',
            'per_year': [], 'skipped_years': skipped_years,
        }

    pool_sorted = sorted(pool)
    n = len(pool_sorted)
    below = sum(1 for v in pool_sorted if v <= price)
    pct = round(below / n * 100, 1)
    median = pool_sorted[n // 2] if n % 2 else (pool_sorted[n // 2 - 1] + pool_sorted[n // 2]) / 2
    # 十分位用于前端画“刻度尺”
    deciles = [round(pool_sorted[int(n * d / 10) - 1 if d > 0 else 0], 2) for d in range(0, 11)]
    return {
        'ok': True,
        'code': code,
        'underlying': underlying,
        'delivery_month': f'{cur_year}-{mm:02d}',
        'price': price,
        'price_from': price_from,
        'stats': {
            'days': n, 'min': pool_sorted[0], 'max': pool_sorted[-1],
            'avg': round(sum(pool_sorted) / n, 2), 'median': median,
            'pct': pct, 'below': below, 'deciles': deciles,
        },
        'per_year': per_year,
        'skipped_years': skipped_years,
    }


# ---------- 触发抓取任务（需登录） ----------


@router.post('/api/futures/refresh-contracts')
def api_refresh_contracts(payload: dict | None = None, user: User = Depends(get_current_user)):
    """刷新期货合约库在市合约（新浪当前挂牌，幂等）。

    只添加新挂牌合约 + 把本次成功抓取交易所里没再出现的在市合约置 is_active=0；
    历史退市合约由 build_futures_base_history.py 另行补录。

    body 可选：{"dry_run": false}（true 只试跑不写库）。
    立即返回：{"ok": true, "job_id": "..."}，用 GET /api/futures/jobs/{job_id} 查进度。
    """
    dry_run = bool((payload or {}).get('dry_run'))
    job_id = _start_job('refresh_contracts', {'dry_run': dry_run})
    return {'ok': True, 'job_id': job_id, 'status': 'running'}


@router.post('/api/futures/fetch-history')
def api_fetch_history(payload: dict | None = None, user: User = Depends(get_current_user)):
    """抓取国内期货日级历史行情入库（幂等、可增量，全量约 10 分钟）。

    body 全部可选：
      {"symbols": "RB2701,RB0" | ["RB2701","RB0"],   # 缺省=futures_base 全部具体合约
       "active_only": true,     # 只抓在市合约（缺省清单模式默认 true）
       "limit": 0,              # 只处理前 N 个合约（试跑用）
       "full_refresh": false,   # 忽略增量起点全量 upsert
       "sleep": 0.3,            # 请求间隔秒数
       "dry_run": false}
    立即返回：{"ok": true, "job_id": "..."}，用 GET /api/futures/jobs/{job_id} 查进度。
    """
    p = payload or {}
    symbols = p.get('symbols')
    if isinstance(symbols, str):
        symbols = [s.strip().upper() for s in symbols.split(',') if s.strip()]
    job_id = _start_job('fetch_history', {
        'symbols': symbols,
        'active_only': bool(p.get('active_only', True)),
        'limit': int(p.get('limit', 0) or 0),
        'full_refresh': bool(p.get('full_refresh', False)),
        'sleep': float(p.get('sleep', SLEEP) or SLEEP),
        'dry_run': bool(p.get('dry_run', False)),
    })
    return {'ok': True, 'job_id': job_id, 'status': 'running'}



@router.get('/api/futures/jobs/{job_id}')
def api_job_status(job_id: str, user: User = Depends(get_current_user)):
    """查询后台抓取任务状态。

    返回：{"job_id":..., "kind":..., "status": "running|done|error",
          "created_at":..., "finished_at":..., "lines":[...最近日志], "result":..., "error":...}
    任务记录在内存中，服务重启后不可查（返回 404）。
    """
    record = JOBS.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail='任务不存在或服务已重启（任务记录在内存）')
    return {k: record[k] for k in (
        'job_id', 'kind', 'status', 'created_at', 'finished_at', 'lines', 'result', 'error'
    )}


@router.get('/api/history/dailybars')
def history_dailybars(symbol: str = '', limit: int = 0,
                      db: Session = Depends(get_db)):
    """历史日K（数据库出接口）：从 futures_daily_bars 读某合约日K，按日期升序。

    /api/history/dailybars?symbol=RB2701&limit=0

    数据由定时导入（fetch_daily_history.py / POST /api/futures/fetch-history）
    写入。库中暂无该合约时返回 ok=false + reason，前端可回退到实时网页日K。
    返回：{ok, symbol, rows:[{date,open,high,low,close,volume,open_interest,settlement}]}
    """
    sym = (symbol or '').strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail='缺少 symbol 参数')
    stmt = (select(FuturesDailyBar)
            .where(FuturesDailyBar.symbol == sym)
            .order_by(FuturesDailyBar.trade_date))
    if limit and limit > 0:
        stmt = stmt.limit(limit)
    rows = db.scalars(stmt).all()
    if not rows:
        return {'ok': False, 'symbol': sym,
                'reason': '库中暂无该合约日K（请先跑 fetch_daily_history.py 定时导入）',
                'rows': []}
    return {'ok': True, 'symbol': sym, 'rows': [{
        'date': str(r.trade_date),
        'open': r.open, 'high': r.high, 'low': r.low, 'close': r.close,
        'volume': r.volume, 'open_interest': r.open_interest,
        'settlement': r.settlement,
    } for r in rows]}


# =====================================================================
# 品种/合约库（原 futures_base.py 合并而来）
# =====================================================================
def validate_position_code(code: str, db: Session) -> tuple[bool, str | None]:
    """校验持仓 code 是否合法。返回 (ok, 错误信息)。
    - nf_ 前缀 → 必须在表内且 is_active，否则拒绝
    - 其他前缀（hf_/sh/sz/bj/hk）→ 放行（不在本表校验范围）
    """
    c = (code or '').strip()
    if c.startswith('nf_'):
        fb = db.scalar(select(FuturesBase).where(
            FuturesBase.code == c, FuturesBase.is_active.is_(True)
        ))
        if fb is None:
            return False, f'合约 {c} 不在可交易清单中（可能已到期下架或不存在）'
        return True, None
    # 纯字母（1~4 位）：可能是国内品种名（如 RB），缺月份，无法精确对应合约，
    # 直接拒绝，避免被前端归一化成海外期货而误放行。
    if re.fullmatch(r'[A-Za-z]{1,4}', c):
        return False, f'「{c}」只是品种名，请填写完整合约代码（如 RB2701）或从联想列表选择'
    # 其他前缀（hf_/sh/sz/bj/hk/6位数字）→ 放行（不在本表校验范围）
    return True, None


def auto_fill_multiplier(code: str, db: Session) -> float | None:
    """若持仓未传 multiplier，且 code 是国内期货，则从合约库自动补乘数。"""
    c = (code or '').strip()
    if not c.startswith('nf_'):
        return None
    fb = db.scalar(select(FuturesBase).where(FuturesBase.code == c))
    return fb.multiplier if fb else None


# ========== HTTP 端点 ==========

@router.get("/api/futures-base", response_model=list[FuturesBaseOut])
def list_futures_base(
    underlying: str | None = Query(None, description="按品种代码筛选，如 RB"),
    active_only: bool = Query(True, description="只列在市合约；false 连历史退市合约一起列"),
    db: Session = Depends(get_db),
):
    """列出合约库条目（默认只在市）。前端拿下拉框/联想，或按品种筛选。"""
    stmt = select(FuturesBase).order_by(
        FuturesBase.exchange, FuturesBase.underlying, FuturesBase.symbol
    )
    if active_only:
        stmt = stmt.where(FuturesBase.is_active.is_(True))
    if underlying:
        stmt = stmt.where(FuturesBase.underlying == underlying.upper())
    return db.scalars(stmt).all()


@router.get("/api/futures-base/search", response_model=list[FuturesBaseOut])
def search_futures_base(
    key: str = Query("", description="关键字：匹配 symbol/name/underlying/code"),
    limit: int = Query(30, ge=1, le=100, description="最多返回条数"),
    db: Session = Depends(get_db),
):
    """按关键字搜索在市合约（新增持仓联想）。空关键字返回空列表。

    匹配优先级：symbol 前缀命中（如 RB -> RB2701...）排最前，其余按
    交易所/品种/合约排序。
    """
    kw = (key or '').strip()
    if not kw:
        return []
    like = f"%{kw.upper()}%"
    name_like = f"%{kw}%"
    prefix_like = f"{kw.upper()}%"
    stmt = (
        select(FuturesBase)
        .where(FuturesBase.is_active.is_(True))
        .where(or_(
            FuturesBase.symbol.ilike(like),
            FuturesBase.code.ilike(like),
            FuturesBase.underlying.ilike(like),
            FuturesBase.name.ilike(name_like),
            FuturesBase.underlying_name.ilike(name_like),
        ))
        .order_by(
            # symbol 前缀命中（含完全相等）优先，其余靠后
            FuturesBase.symbol.ilike(prefix_like).desc(),
            FuturesBase.exchange,
            FuturesBase.underlying,
            FuturesBase.symbol,
        )
        .limit(limit)
    )
    return db.scalars(stmt).all()


@router.get("/api/futures-base/{code}", response_model=FuturesBaseOut)
def get_futures_base(code: str, db: Session = Depends(get_db)):
    """查单个合约（传完整 code 如 nf_RB2701；历史退市合约也能查到）。"""
    fb = db.scalar(select(FuturesBase).where(FuturesBase.code == code.upper()))
    if fb is None:
        raise HTTPException(status_code=404, detail="合约不存在")
    return fb


@router.post("/api/futures-base/validate")
def validate_codes(payload: dict, db: Session = Depends(get_db)):
    """批量校验 code 列表。前端新增持仓前可一次性校验。
    body: {"codes": ["nf_RB2701", "nf_XX9999", "hf_OIL"]}
    返回: {"results": [{"code":..., "ok":true|false, "reason":...}, ...]}
    """
    codes = payload.get("codes") or []
    if not isinstance(codes, list):
        raise HTTPException(status_code=400, detail="codes 必须是数组")
    results = []
    for c in codes:
        ok, reason = validate_position_code(str(c), db)
        results.append({"code": c, "ok": ok, "reason": reason})
    return {"results": results}

