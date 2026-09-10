"""期货合约目录 HTTP 接口。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import FuturesBase
from app.schemas import FuturesBaseOut
from app.services.futures_catalog_service import refresh_contracts
from app.utils.contract_codes_utils import is_live_symbol, validate_position_code

router = APIRouter(tags=["futures-catalog"])


@router.get("/api/futures-base", response_model=list[FuturesBaseOut])
def list_futures_base(
    underlying: str | None = Query(None, description="按品种代码筛选，如 RB"),
    active_only: bool = Query(True, description="默认只看当前可交易合约"),
    db: Session = Depends(get_db),
):
    stmt = select(FuturesBase).order_by(
        FuturesBase.exchange, FuturesBase.underlying, FuturesBase.symbol
    )
    if underlying:
        stmt = stmt.where(FuturesBase.underlying == underlying.upper())
    rows = db.scalars(stmt).all()
    return [row for row in rows if not active_only or is_live_symbol(row.symbol)]


@router.get("/api/futures-base/search", response_model=list[FuturesBaseOut])
def search_futures_base(
    key: str = Query("", description="匹配 symbol/name/underlying/code"),
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
):
    keyword = (key or "").strip()
    if not keyword:
        return []
    like = f"%{keyword.upper()}%"
    name_like = f"%{keyword}%"
    prefix_like = f"{keyword.upper()}%"
    stmt = (
        select(FuturesBase)
        .where(or_(
            FuturesBase.symbol.ilike(like), FuturesBase.code.ilike(like),
            FuturesBase.underlying.ilike(like), FuturesBase.name.ilike(name_like),
            FuturesBase.underlying_name.ilike(name_like),
        ))
        .order_by(
            FuturesBase.symbol.ilike(prefix_like).desc(),
            FuturesBase.exchange, FuturesBase.underlying, FuturesBase.symbol,
        )
        .limit(limit * 4)
    )
    rows = db.scalars(stmt).all()
    return [row for row in rows if is_live_symbol(row.symbol)][:limit]


@router.get("/api/futures-base/{code}", response_model=FuturesBaseOut)
def get_futures_base(code: str, db: Session = Depends(get_db)):
    item = db.scalar(select(FuturesBase).where(func.lower(FuturesBase.code) == code.lower()))
    if item is None:
        raise HTTPException(status_code=404, detail="合约不存在")
    return item


@router.post("/api/futures-base/refresh")
def refresh_futures_base(
    dry_run: bool = Query(False, description="true=只统计不写库"),
    db: Session = Depends(get_db),
):
    """把当前挂牌合约补进 futures_base（akshare，幂等 upsert，只增不删）。

    遍历全部品种逐个拉合约，约需 30~60 秒；返回结果与脚本一致
    （inserted/updated/unchanged/skipped/failed/total）。
    """
    try:
        return refresh_contracts(db, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"合约目录刷新失败: {exc}")


@router.post("/api/futures-base/validate")
def validate_codes(payload: dict):
    codes = payload.get("codes") or []
    if not isinstance(codes, list):
        raise HTTPException(status_code=400, detail="codes 必须是数组")
    results = []
    for code in codes:
        ok, reason = validate_position_code(str(code))
        results.append({"code": code, "ok": ok, "reason": reason})
    return {"results": results}
