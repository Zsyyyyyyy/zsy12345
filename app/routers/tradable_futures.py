#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国内可交易期货「真实合约」路由 —— 行情看板「新增持仓」校验 + 前端下拉框。

表 tradable_futures 存的是当前挂牌的真实合约（如 nf_RB2701），
由 refresh_tradable_futures.py 每天从新浪刷新一次。

接口：
  GET  /api/tradable-futures                列出全部在交易合约（可按 underlying 筛选）
  GET  /api/tradable-futures/{code}         查单个合约
  POST /api/tradable-futures/validate       校验 code 是否合法（批量），body={"codes":["nf_RB2701",...]}

提供模块级工具函数 `validate_position_code(code, db)` 给 positions.py 复用：
  - nf_ 前缀 → 精确匹配表内 is_active 合约，命中才通过
  - hf_/sz/sh/bj/hk 等其他 code → 直接通过（不在校验范围）
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import TradableFuture
from app.schemas import TradableFutureOut

router = APIRouter(tags=["tradable-futures"])

# 国内期货 code 形如 nf_<underlying><4位年月>，如 nf_RB2701
_DOM_CODE_RE = re.compile(r'^nf_([A-Za-z]+)\d{4}$')


# ========== 公共工具（供 positions.py 复用） ==========

def validate_position_code(code: str, db: Session) -> tuple[bool, str | None]:
    """校验持仓 code 是否合法。返回 (ok, 错误信息)。
    - nf_ 前缀 → 必须在表内且 is_active，否则拒绝
    - 其他前缀（hf_/sh/sz/bj/hk）→ 放行（不在本表校验范围）
    """
    c = (code or '').strip()
    if not c.startswith('nf_'):
        return True, None
    tf = db.scalar(select(TradableFuture).where(
        TradableFuture.code == c, TradableFuture.is_active.is_(True)
    ))
    if tf is None:
        return False, f'合约 {c} 不在可交易清单中（可能已到期下架或不存在）'
    return True, None


def auto_fill_multiplier(code: str, db: Session) -> float | None:
    """若持仓未传 multiplier，且 code 是国内期货，则从合约表自动补乘数。"""
    c = (code or '').strip()
    if not c.startswith('nf_'):
        return None
    tf = db.scalar(select(TradableFuture).where(TradableFuture.code == c))
    return tf.multiplier if tf else None


# ========== HTTP 端点 ==========

@router.get("/api/tradable-futures", response_model=list[TradableFutureOut])
def list_tradable_futures(
    underlying: str | None = Query(None, description="按品种代码筛选，如 RB"),
    active_only: bool = True,
    db: Session = Depends(get_db),
):
    """列出全部在交易合约。前端拿下拉框/联想，或按品种筛选。"""
    stmt = select(TradableFuture).order_by(
        TradableFuture.exchange, TradableFuture.underlying, TradableFuture.symbol
    )
    if active_only:
        stmt = stmt.where(TradableFuture.is_active.is_(True))
    if underlying:
        stmt = stmt.where(TradableFuture.underlying == underlying.upper())
    return db.scalars(stmt).all()


@router.get("/api/tradable-futures/{code}", response_model=TradableFutureOut)
def get_tradable_future(code: str, db: Session = Depends(get_db)):
    """查单个合约（传完整 code 如 nf_RB2701）。"""
    tf = db.scalar(select(TradableFuture).where(TradableFuture.code == code.upper()))
    if tf is None:
        raise HTTPException(status_code=404, detail="合约不存在")
    return tf


@router.post("/api/tradable-futures/validate")
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
