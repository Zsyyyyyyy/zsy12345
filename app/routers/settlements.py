#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
结算接口 —— 行情看板「我的持仓」平仓结算，成交快照与盈亏写入结算表，按用户隔离。

结算只针对国内期货（nf_ 开头）持仓：历史遗留的海外期货/港股/股票持仓为
只读记录，不可结算，需先删除。结算币种固定人民币 CNY。

接口（均需登录，Header 带 Authorization: Bearer <token>）：
  POST /api/positions/{pos_id}/settle   结算某条持仓（传结算价 + 可选手数）
                                       不传手数 = 按持仓剩余手数全部平仓；传手数 = 部分结算
                                       剩余手数继续留在 positions 表
  GET  /api/settlements                 读当前用户全部结算记录（最新在前）
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Position, Settlement, User
from app.schemas import SettlementCreate, SettlementOut

router = APIRouter(tags=["settlements"])


@router.post("/api/positions/{pos_id}/settle", response_model=SettlementOut, status_code=status.HTTP_201_CREATED)
def settle_position(
    pos_id: int,
    data: SettlementCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """结算持仓：按结算价计算盈亏并落表（仅支持国内期货持仓）。
    - 不传 `lots`：按持仓全部手数平仓，写完结算后删除该持仓。
    - 传 `lots`：部分结算（0 < lots < pos.lots），剩余手数扣减留在持仓表；满额时等同全平。
    """
    pos = db.scalar(select(Position).where(Position.id == pos_id, Position.user_id == user.id))
    if pos is None:
        raise HTTPException(status_code=404, detail="持仓不存在")

    # 历史遗留的非国内期货持仓只读：不可结算（可删除）
    if not pos.code.startswith('nf_'):
        raise HTTPException(
            status_code=400,
            detail='该持仓为历史遗留的非国内期货记录，仅支持查看和删除，不能结算',
        )

    # 手数：请求显式传入 > 持仓全部手数（缺省视为全平）
    settle_lots = float(data.lots) if data.lots is not None else float(pos.lots)
    if settle_lots <= 0 or settle_lots > float(pos.lots) + 1e-9:
        raise HTTPException(status_code=400, detail=f"结算手数必须在 (0, {pos.lots}] 之间")

    # 乘数：请求显式传入 > 持仓快照 > 默认 1
    multiplier = data.multiplier
    if multiplier is None:
        multiplier = pos.multiplier if pos.multiplier is not None else 1.0

    # 方向：请求显式传入 > 持仓快照 > 默认做多
    direction = (data.direction or getattr(pos, "direction", None) or "long").lower()
    if direction not in ("long", "short"):
        direction = "long"

    # 国内期货结算币种固定人民币（历史旧表里的 USD/HKD 记录仅展示保留）
    currency = "CNY"
    # 做多低买高卖、做空高卖低买，方向决定价差正负的取法
    price_diff = (data.settle_price - pos.buy_price) if direction == "long" else (pos.buy_price - data.settle_price)
    pnl = price_diff * settle_lots * float(multiplier)

    record = Settlement(
        user_id=user.id,
        code=pos.code,
        direction=direction,
        name=data.name,
        buy_price=pos.buy_price,
        settle_price=data.settle_price,
        spread=price_diff,
        lots=settle_lots,
        multiplier=multiplier,
        pnl=pnl,
        currency=currency,
    )
    db.add(record)

    # 部分结算：扣减持仓剩余手数；满额：删除持仓
    if settle_lots + 1e-9 >= float(pos.lots):
        db.delete(pos)
    else:
        pos.lots = float(pos.lots) - settle_lots
        # 显式触发 onupdate=func.now()，避免脏检查判「无变化」跳过 UPDATE
        from sqlalchemy import func as _func
        pos.updated_at = _func.now()

    db.commit()
    db.refresh(record)
    return record


@router.get("/api/settlements", response_model=list[SettlementOut])
def list_settlements(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读当前用户全部结算记录，最新在前。"""
    return db.scalars(
        select(Settlement).where(Settlement.user_id == user.id).order_by(Settlement.settled_at.desc(), Settlement.id.desc())
    ).all()
