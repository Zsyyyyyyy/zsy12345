#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
history.py —— 历史行情 / 品种信息接口（读数据库），全部返回 JSON

品种/合约库 futures_base（纯只读接口；表数据由 refresh_tradable_futures.py
脚本定时导入「当前挂牌的新合约」，历史到期合约一直保留、不做下架删除）：
  GET  /api/futures-base                列出合约库（默认只看「当前可交易」）
  GET  /api/futures-base/search         搜索可交易合约（持仓代码联想用）
  GET  /api/futures-base/{code}         查单个合约（含历史到期）
  POST /api/futures-base/validate       批量校验 code 当前是否可交易
历史行情：
  GET  /api/futures/hist-position       当前价在历年同月合约区间位置（读库，缺的现抓回填）
  GET  /api/history/dailybars           从 futures_daily_bars 出历史日K（读库）

已删除：is_active 列与 /api/futures/refresh-contracts、/api/futures/fetch-history、
/api/futures/jobs/{id} 后台任务接口，以及历史导入脚本。

「当前可交易」统一按 symbol 交割年月判断：交割月 >= 当前月 即算可交易
（如 2027-02 时 RB2701 到期不可用，01 合约自然轮到 RB2801），
判定逻辑见 _is_live_symbol / validate_position_code。
"""
import json
import re
import time
import urllib.parse
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.fetchutils import (BATCH_SIZE, CONTRACT_URL, EXCHANGE_MAP, MULTIPLIERS,
                            NODE_LIST_URL, SLEEP, _CONTRACT_RE, http_get, parse_jsonp)
from app.models import FuturesBase, FuturesDailyBar
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
    """插入或更新一个合约。返回 inserted/updated/unchanged。

    表不再有 is_active：入库过的合约（含已到期）永不删除/改状态，
    是否「当前可交易」由 symbol 交割年月 vs 当前日期判断（见 _is_live_symbol）。
    """
    existing = db.scalar(select(FuturesBase).where(FuturesBase.code == code))
    if existing is None:
        if not dry_run:
            db.add(FuturesBase(
                code=code, symbol=symbol, name=name, underlying=underlying,
                underlying_name=underlying_name, exchange=exchange,
                multiplier=multiplier, tick_size=tick_size,
            ))
        return 'inserted'
    changed = False
    for attr, val in [
        ('symbol', symbol), ('name', name), ('underlying', underlying),
        ('underlying_name', underlying_name), ('exchange', exchange),
        ('multiplier', multiplier), ('tick_size', tick_size),
    ]:
        if getattr(existing, attr) != val:
            if not dry_run:
                setattr(existing, attr, val)
            changed = True
    return 'updated' if changed else 'unchanged'


def refresh_contracts(db: Session, dry_run: bool = False, log=None) -> dict:
    """刷新 futures_base 合约库：只加不删（供 refresh_tradable_futures.py 定时调用）。

    新浪当前挂牌、表里还没有的合约补进去（幂等 upsert，同时刷新名称/乘数字段）；
    已存在的（含到期后不再挂牌的）一律保留、不改状态——是否「当前可交易」由调用方
    按 symbol 交割年月 vs 当前日期判断，不再维护 is_active，也不做下架扫描。

    log 为可调用对象（接收一行文本）或 None（静默，供 --json 用）。
    返回统计 dict（可直接 JSON 序列化）。
    """
    def say(msg: str) -> None:
        if log is not None:
            log(msg)

    nodes = fetch_nodes()
    say(f'品种 node 数：{len(nodes)}')
    inserted = updated = unchanged = skipped = failed = 0

    for cn_name, node, exchange in nodes:
        try:
            contracts = fetch_contracts(node)
        except Exception as e:
            say(f'  ✗ {cn_name:8} node={node:12} 拉取失败：{e}')
            failed += 1
            continue

        for c in contracts:
            symbol = (c.get('symbol') or '').upper()
            m = _CONTRACT_RE.match(symbol)
            if not m:
                # 连续合约（RB0）或异常 symbol，跳过
                skipped += 1
                continue
            underlying = m.group(1)
            code = 'nf_' + symbol
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

    if not dry_run:
        db.commit()
    total = len(db.scalars(select(FuturesBase)).all())

    say('-' * 60)
    say(f'新增 {inserted} / 更新 {updated} / 未变 {unchanged} / '
        f'跳过(连续等) {skipped} / 拉取失败品种 {failed}')
    if not dry_run:
        say(f'✅ 完成：合约库共 {total} 条（是否可交易由交割月份判断，本脚本不再维护）')
    else:
        say('[DRY-RUN] 未提交')
    return {
        'dry_run': dry_run, 'nodes': len(nodes),
        'inserted': inserted, 'updated': updated, 'unchanged': unchanged,
        'skipped': skipped, 'failed': failed,
        'total': total,
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


@router.get('/api/history/dailybars')
def history_dailybars(symbol: str = '', limit: int = 0,
                      db: Session = Depends(get_db)):
    """历史日K（数据库出接口）：从 futures_daily_bars 读某合约日K，按日期升序。

    /api/history/dailybars?symbol=RB2701&limit=0

    futures_daily_bars 由「历史价格位置」查询按需回填（见 /api/futures/hist-position）；
    库中暂无该合约时返回 ok=false + reason，前端可回退到实时网页日K。
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
                'reason': '库中暂无该合约日K（可访问 /api/futures/hist-position 按需回填）',
                'rows': []}
    return {'ok': True, 'symbol': sym, 'rows': [{
        'date': str(r.trade_date),
        'open': r.open, 'high': r.high, 'low': r.low, 'close': r.close,
        'volume': r.volume, 'open_interest': r.open_interest,
        'settlement': r.settlement,
    } for r in rows]}


# =====================================================================
# 品种/合约库 futures_base（纯只读接口；无 is_active，「可交易」按交割年月判断）
# =====================================================================
def _is_live_symbol(symbol: str) -> bool:
    """交割月 >= 当前月 即视为「当前可交易」（2027-02 时 RB2701 已到期）。
    非「字母+4位年月」的码（连续 RB0 等）不适用该规则，一律放行。
    """
    m = _CONTRACT_RE.match((symbol or '').upper())
    if not m:
        return True
    yy, mm = int(m.group(2)[:2]), int(m.group(2)[2:])
    if not 1 <= mm <= 12:
        return True
    today = date.today()
    return date(2000 + yy, mm, 1) >= today.replace(day=1)


def validate_position_code(code: str, db: Session) -> tuple[bool, str | None]:
    """校验持仓 code 是否合法。返回 (ok, 错误信息)。

    持仓只考虑国内期货，且按 symbol 判断「当前可交易」——不再依赖 futures_base
    表与 is_active：
    - nf_ 开头 + 品种 + 4 位交割年月，交割月 >= 当前月 → 通过；
      例如 2027-02 时 RB2701（2027-01 交割）已到期不可用，01 合约轮到 RB2801。
    - 海外期货(hf_)、港股(hk)、A股证券(sh/sz/bj)、纯字母品种名一律拒绝。
    db 参数保留以兼容旧调用方，本函数不再读表。
    """
    c = (code or '').strip()
    if c.startswith('nf_'):
        sym = c[4:].upper()
        m = _CONTRACT_RE.match(sym)
        if not m:
            return False, f'「{c}」不是有效合约：应形如 nf_RB2701（品种 + 4 位交割年月）'
        yy, mm = int(m.group(2)[:2]), int(m.group(2)[2:])
        if not 1 <= mm <= 12:
            return False, f'「{c}」的月份不合法（应为 01~12，如 nf_RB2701）'
        today = date.today()
        if date(2000 + yy, mm, 1) < today.replace(day=1):
            return False, (f'合约 {c} 已到期（交割月 20{yy:02d}年{mm:02d} 早于当前 '
                           f'{today.year}年{today.month:02d}月）。可交易判断：交割月 >= 当前月，'
                           f'如 01 合约在到期后要选下一年的（RB2701 → RB2801），请从联想列表选择')
        return True, None
    # 纯字母（1~4 位）：可能是国内品种名（如 RB），缺月份，无法精确对应合约
    if re.fullmatch(r'[A-Za-z]{1,4}', c):
        return False, f'「{c}」只是品种名，请填写完整国内期货合约代码（如 RB2701）或从联想列表选择'
    if c.startswith('hf_'):
        return False, '海外期货已下线：持仓/结算仅支持国内期货（如 nf_RB2701）'
    if c.lower().startswith('hk'):
        return False, '港股已下线：持仓/结算仅支持国内期货（如 nf_RB2701）'
    if re.match(r'^(sh|sz|bj)\d{6}$', c.lower()):
        return False, '股票/指数不支持记持仓：持仓/结算仅支持国内期货（如 nf_RB2701）'
    return False, f'「{c}」不是有效的国内期货合约代码（应为 nf_ 开头，如 nf_RB2701）'


def auto_fill_multiplier(code: str, db: Session) -> float | None:
    """持仓未传 multiplier 时，按品种乘数字典自动补（不再查合约库）。"""
    c = (code or '').strip()
    if not c.startswith('nf_'):
        return None
    m = _CONTRACT_RE.match(c[4:].upper())
    if not m:
        return None
    v = MULTIPLIERS.get(m.group(1))
    return v[0] if v else None


# ========== HTTP 端点 ==========

@router.get("/api/futures-base", response_model=list[FuturesBaseOut])
def list_futures_base(
    underlying: str | None = Query(None, description="按品种代码筛选，如 RB"),
    active_only: bool = Query(True, description="默认只看交割月 >= 当月（当前可交易）；false 连历史到期合约一起列"),
    db: Session = Depends(get_db),
):
    """列出合约库条目。默认按交割年月过滤出「当前可交易」合约，前端拿下拉框/联想。"""
    stmt = select(FuturesBase).order_by(
        FuturesBase.exchange, FuturesBase.underlying, FuturesBase.symbol
    )
    if underlying:
        stmt = stmt.where(FuturesBase.underlying == underlying.upper())
    rows = db.scalars(stmt).all()
    if active_only:
        rows = [r for r in rows if _is_live_symbol(r.symbol)]
    return rows


@router.get("/api/futures-base/search", response_model=list[FuturesBaseOut])
def search_futures_base(
    key: str = Query("", description="关键字：匹配 symbol/name/underlying/code"),
    limit: int = Query(30, ge=1, le=100, description="最多返回条数"),
    db: Session = Depends(get_db),
):
    """按关键字搜索「当前可交易」合约（新增持仓联想）。空关键字返回空列表。

    只返回交割月 >= 当月的合约：RB2701 到期后不再出现，联想里的 01 合约
    自然轮到 RB2801。匹配优先级：symbol 前缀命中（如 RB -> RB2701...）排最前。
    """
    kw = (key or '').strip()
    if not kw:
        return []
    like = f"%{kw.upper()}%"
    name_like = f"%{kw}%"
    prefix_like = f"{kw.upper()}%"
    stmt = (
        select(FuturesBase)
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
        .limit(limit * 4)  # 多取一些，过滤掉已到期合约后仍够 limit
    )
    rows = db.scalars(stmt).all()
    return [r for r in rows if _is_live_symbol(r.symbol)][:limit]


@router.get("/api/futures-base/{code}", response_model=FuturesBaseOut)
def get_futures_base(code: str, db: Session = Depends(get_db)):
    """查单个合约（传完整 code 如 nf_RB2701；历史到期合约也能查到，不带状态）。"""
    fb = db.scalar(select(FuturesBase).where(func.lower(FuturesBase.code) == code.lower()))
    if fb is None:
        raise HTTPException(status_code=404, detail="合约不存在")
    return fb


@router.post("/api/futures-base/validate")
def validate_codes(payload: dict, db: Session = Depends(get_db)):
    """批量校验 code 是否「当前可交易」（按交割年月规则，不查库）。前端新增持仓前调用。
    body: {"codes": ["nf_RB2701", "nf_RB2801", "sh600519"]}
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

