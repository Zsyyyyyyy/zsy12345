"""实时行情 HTTP 接口；新浪协议细节统一由 clients.sina_client 处理。"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.clients.sina_client import (
    get_daily_kline,
    get_minute_line,
    get_quotes,
    search_symbols,
)

router = APIRouter(tags=["quotes"])


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
