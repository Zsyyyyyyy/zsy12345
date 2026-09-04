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
from sqlalchemy import or_, select
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
    if c.startswith('nf_'):
        tf = db.scalar(select(TradableFuture).where(
            TradableFuture.code == c, TradableFuture.is_active.is_(True)
        ))
        if tf is None:
            return False, f'合约 {c} 不在可交易清单中（可能已到期下架或不存在）'
        return True, None
    # 纯字母（1~4 位）：可能是国内品种名（如 RB），缺月份，无法精确对应合约，
    # 直接拒绝，避免被前端归一化成海外期货而误放行。
    if re.fullmatch(r'[A-Za-z]{1,4}', c):
        return False, f'「{c}」只是品种名，请填写完整合约代码（如 RB2701）或从联想列表选择'
    # 其他前缀（hf_/sh/sz/bj/hk/6位数字）→ 放行（不在本表校验范围）
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


@router.get("/api/tradable-futures/search", response_model=list[TradableFutureOut])
def search_tradable_futures(
    key: str = Query("", description="关键字：匹配 symbol/name/underlying/code"),
    limit: int = Query(30, ge=1, le=100, description="最多返回条数"),
    db: Session = Depends(get_db),
):
    """按关键字搜索可交易合约（新增持仓联想）。空关键字返回空列表。

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
        select(TradableFuture)
        .where(TradableFuture.is_active.is_(True))
        .where(or_(
            TradableFuture.symbol.ilike(like),
            TradableFuture.code.ilike(like),
            TradableFuture.underlying.ilike(like),
            TradableFuture.name.ilike(name_like),
            TradableFuture.underlying_name.ilike(name_like),
        ))
        .order_by(
            # symbol 前缀命中（含完全相等）优先，其余靠后
            TradableFuture.symbol.ilike(prefix_like).desc(),
            TradableFuture.exchange,
            TradableFuture.underlying,
            TradableFuture.symbol,
        )
        .limit(limit)
    )
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
