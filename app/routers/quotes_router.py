"""实时行情 HTTP 接口；/api/futures 实时行情走 akshare（只处理国内期货 nf_ 代码）。

K线/分钟线/联想仍走新浪 stock2 域（与被封的 hq.sinajs.cn 不是同一个域）。
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.akshare_client import get_quotes
from app.clients.sina_client import (
    get_daily_kline,
    get_minute_line,
    search_symbols,
)
from app.core.database import get_db
from app.models import FuturesBase

router = APIRouter(tags=["quotes"])


@router.get("/api/futures/margins")
def futures_margins(codes: str = Query(""), db: Session = Depends(get_db)):
    """返回合约交易所最低保证金比例；连续合约取该品种当前库内最低值。"""
    raw_codes = [c.strip().lower() for c in codes.split(",") if c.strip()]
    if not raw_codes:
        raise HTTPException(status_code=400, detail="缺少 codes 参数")
    rows = db.scalars(select(FuturesBase).where(FuturesBase.exchange_margin_rate.is_not(None))).all()
    result = {}
    for code in raw_codes:
        if not code.startswith("nf_"):
            continue
        symbol = code.removeprefix("nf_").upper()
        exact = next((r for r in rows if r.symbol == symbol), None)
        if exact is None:
            underlying = re.match(r"^[A-Z]+", symbol)
            candidates = [r for r in rows if underlying and r.underlying == underlying.group(0)]
            exact = min(candidates, key=lambda r: r.exchange_margin_rate) if candidates else None
        if exact is not None:
            result[code] = {
                "rate": exact.exchange_margin_rate,
                "updated_at": exact.margin_updated_at,
                "source": exact.margin_source,
            }
    # 交给 FastAPI 编码 datetime 等 ORM 字段，避免 JSONResponse 直接序列化时报 500。
    return {"items": result}


@router.get("/api/futures")
def futures(codes: str = ""):
    if not codes:
        raise HTTPException(status_code=400, detail="缺少 codes 参数")
    return JSONResponse({"items": get_quotes(codes.split(","))})


@router.get("/api/futures/suggest")
def futures_suggest(key: str = ""):
    if not key:
        raise HTTPException(status_code=400, detail="缺少 key 参数")
    return JSONResponse({"items": search_symbols(key)})


@router.get("/api/futures/minline")
def futures_minline(symbol: str = ""):
    if not symbol:
        raise HTTPException(status_code=400, detail="缺少 symbol 参数")
    return JSONResponse({"symbol": symbol, "data": get_minute_line(symbol)})


@router.get("/api/futures/dailykline")
def futures_dailykline(symbol: str = ""):
    if not symbol:
        raise HTTPException(status_code=400, detail="缺少 symbol 参数")
    return JSONResponse({"symbol": symbol, "data": get_daily_kline(symbol)})
