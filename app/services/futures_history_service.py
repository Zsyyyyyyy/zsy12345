"""期货历史 K 线的解析与批量写入。"""
import re
from datetime import date

from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.fetchutils import BATCH_SIZE
from app.models import FuturesDailyBar
from app.utils.contract_codes_utils import contract_month_of


def _to_float(value) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else None
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    parsed = _to_float(value)
    return None if parsed is None else int(parsed)


def parse_kline_rows(symbol: str, data: list[dict]) -> list[dict]:
    month = contract_month_of(symbol)
    match = re.match(r"^([A-Za-z]+)", (symbol or "").strip())
    underlying = match.group(1).upper() if match else None
    rows = []
    for item in data:
        try:
            trade_date = date.fromisoformat((item.get("d") or "").strip())
        except ValueError:
            continue
        close = _to_float(item.get("c"))
        if close is None or close <= 0:
            continue
        rows.append({
            "symbol": symbol, "underlying": underlying, "trade_date": trade_date,
            "contract_month": month, "open_price": _to_float(item.get("o")),
            "high": _to_float(item.get("h")), "low": _to_float(item.get("l")),
            "close": close, "volume": _to_int(item.get("v")),
            "open_interest": _to_int(item.get("p")),
            "settlement": _to_float(item.get("s")) or None,
        })
    return rows


def upsert_daily_bars(db: Session, rows: list[dict], dry_run: bool = False) -> int:
    if not rows or dry_run:
        return len(rows) if dry_run else 0
    written = 0
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        stmt = mysql_insert(FuturesDailyBar).values(batch)
        stmt = stmt.on_duplicate_key_update(
            underlying=stmt.inserted.underlying,
            contract_month=stmt.inserted.contract_month,
            open_price=stmt.inserted.open_price, high=stmt.inserted.high,
            low=stmt.inserted.low, close=stmt.inserted.close,
            volume=stmt.inserted.volume, open_interest=stmt.inserted.open_interest,
            settlement=stmt.inserted.settlement,
        )
        db.execute(stmt)
        written += len(batch)
    db.commit()
    return written
