"""实时行情 HTTP 接口；行情、K线、分时、联想全部走东方财富（只处理国内期货 nf_ 代码）。"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.eastmoney_client import diagnose, get_quotes, num_str
from app.clients.eastmoney_history_client import (
    get_daily_kline,
    get_minute_line,
    search_symbols,
)
from app.core.database import get_db
from app.models import FuturesBase, FuturesDailyBar

router = APIRouter(tags=["quotes"])


def _db_daily_rows(db: Session, symbol: str) -> list[dict]:
    """从本地 futures_daily_bars 读日K，形状与东财客户端一致（[{d,o,h,l,c,v,p,s}]）。

    用途：部分网络只放通东财延迟站（push2delay），而延迟站不提供日K，
    此时退回本地库，避免日K图整块空白。库里没有就返回空列表。
    """
    prefix = re.match(r"^[A-Za-z]+", (symbol or "").strip())
    if not prefix:
        return []
    want = prefix.group(0).upper()
    stmt = (
        select(FuturesDailyBar)
        .where(FuturesDailyBar.symbol == (symbol or "").strip().upper())
        .order_by(FuturesDailyBar.trade_date)
    )
    rows = db.scalars(stmt).all()
    if not rows and want:
        # 大小写/前缀不一致时再按品种兜底匹配一次（库里 symbol 统一大写）
        stmt = (
            select(FuturesDailyBar)
            .where(FuturesDailyBar.underlying == want)
            .order_by(FuturesDailyBar.trade_date)
        )
        rows = db.scalars(stmt).all()
    return [
        {
            "d": r.trade_date.isoformat() if r.trade_date else "",
            "o": num_str(r.open),
            "h": num_str(r.high),
            "l": num_str(r.low),
            "c": num_str(r.close),
            "v": num_str(r.volume),
            "p": num_str(r.open_interest),
            "s": num_str(r.settlement),
        }
        for r in rows
    ]


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


@router.get("/api/futures/diag")
def futures_diag(codes: str = "nf_SA2701"):
    """行情链路自诊断：逐层探测东财实时/合约/品种接口，返回 JSON 报告（排障用）。"""
    return JSONResponse(diagnose([c.strip() for c in codes.split(",") if c.strip()]))


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
def futures_dailykline(symbol: str = "", db: Session = Depends(get_db)):
    if not symbol:
        raise HTTPException(status_code=400, detail="缺少 symbol 参数")
    data = get_daily_kline(symbol)
    source = "eastmoney"
    if not data:
        # 上游日K主机不可达（只放通延迟站的网络）时退回本地库
        data = _db_daily_rows(db, symbol)
        source = "db"
    return JSONResponse({"symbol": symbol, "source": source, "data": data})
