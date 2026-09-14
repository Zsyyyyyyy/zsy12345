"""期货历史行情 HTTP 接口。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.eastmoney_history_client import get_daily_kline
from app.core.database import get_db
from app.models import FuturesDailyBar
from app.services.futures_history_service import parse_kline_rows, upsert_daily_bars
from app.utils.contract_codes_utils import parse_contract_code

router = APIRouter(tags=["futures-history"])

_HIST_SINCE = 2019


def _try_daily_rows(canonical_symbol: str, quote_symbol: str) -> list[dict] | None:
    try:
        data = get_daily_kline(quote_symbol)
    except Exception:  # noqa: BLE001  取不到就交给下一个候选代码，不整单失败
        return None
    if not isinstance(data, list) or not data:
        return None
    rows = parse_kline_rows(canonical_symbol, data)
    return rows or None


def _daily_rows_of_contract(db: Session, underlying: str, year: int, month: int):
    """读取指定交割年月的日 K；数据库没有时从东财回填。"""
    symbol = f"{underlying}{year % 100:02d}{month:02d}"
    stmt = (
        select(FuturesDailyBar)
        .where(FuturesDailyBar.symbol == symbol)
        .order_by(FuturesDailyBar.trade_date)
    )
    rows = db.scalars(stmt).all()
    if rows:
        return rows

    parsed = _try_daily_rows(symbol, symbol)
    if parsed:
        upsert_daily_bars(db, parsed)
        return db.scalars(stmt).all()
    return []


@router.get("/api/futures/hist-position")
def futures_hist_position(
    code: str = "",
    price: float | None = None,
    db: Session = Depends(get_db),
):
    """计算当前价在历年同月合约收盘价区间中的位置。"""
    parsed = parse_contract_code(code)
    if parsed is None:
        raise HTTPException(status_code=400, detail="code 需形如 nf_RB2701（4位年月）")
    underlying, current_year, month = parsed

    price_from = "param"
    if price is None:
        own_rows = _daily_rows_of_contract(db, underlying, current_year, month)
        if not own_rows:
            raise HTTPException(status_code=404, detail=f"{code} 暂无历史数据（可能东财未收录）")
        price = float(own_rows[-1].close)
        price_from = "self-last-close"

    per_year = []
    skipped_years: list[str] = []
    pool: list[float] = []
    for year in range(_HIST_SINCE, current_year):
        rows = _daily_rows_of_contract(db, underlying, year, month)
        if not rows:
            skipped_years.append(f"{year}年{month:02d}月（东财无数据）")
            continue
        closes = [float(row.close) for row in rows if row.close is not None]
        if not closes:
            skipped_years.append(f"{year}年{month:02d}月（无有效收盘）")
            continue
        pool.extend(closes)
        per_year.append({
            "year": year,
            "symbol": f"{underlying}{year % 100:02d}{month:02d}",
            "days": len(closes),
            "min": min(closes),
            "max": max(closes),
            "avg": round(sum(closes) / len(closes), 2),
        })

    if not pool:
        return {
            "ok": False, "code": code, "underlying": underlying,
            "delivery_month": f"{current_year}-{month:02d}", "price": price,
            "reason": "近 7 年无同月历史合约数据（东财未收录更早）",
            "per_year": [], "skipped_years": skipped_years,
        }

    sorted_prices = sorted(pool)
    count = len(sorted_prices)
    below = sum(value <= price for value in sorted_prices)
    median = (
        sorted_prices[count // 2]
        if count % 2
        else (sorted_prices[count // 2 - 1] + sorted_prices[count // 2]) / 2
    )
    deciles = [
        round(sorted_prices[int(count * index / 10) - 1 if index else 0], 2)
        for index in range(11)
    ]
    return {
        "ok": True,
        "code": code,
        "underlying": underlying,
        "delivery_month": f"{current_year}-{month:02d}",
        "price": price,
        "price_from": price_from,
        "stats": {
            "days": count,
            "min": sorted_prices[0],
            "max": sorted_prices[-1],
            "avg": round(sum(sorted_prices) / count, 2),
            "median": median,
            "pct": round(below / count * 100, 1),
            "below": below,
            "deciles": deciles,
        },
        "per_year": per_year,
        "skipped_years": skipped_years,
    }


@router.get("/api/history/dailybars")
def history_dailybars(
    symbol: str = "",
    limit: int = 0,
    db: Session = Depends(get_db),
):
    """从 futures_daily_bars 读取某合约的历史日 K。"""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise HTTPException(status_code=400, detail="缺少 symbol 参数")
    stmt = (
        select(FuturesDailyBar)
        .where(FuturesDailyBar.symbol == normalized)
        .order_by(FuturesDailyBar.trade_date)
    )
    if limit and limit > 0:
        stmt = stmt.limit(limit)
    rows = db.scalars(stmt).all()
    if not rows:
        return {
            "ok": False,
            "symbol": normalized,
            "reason": "库中暂无该合约日K（可访问 /api/futures/hist-position 按需回填）",
            "rows": [],
        }
    return {
        "ok": True,
        "symbol": normalized,
        "rows": [
            {
                "date": str(row.trade_date),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "open_interest": row.open_interest,
                "settlement": row.settlement,
            }
            for row in rows
        ],
    }
