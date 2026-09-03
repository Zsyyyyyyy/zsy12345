#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓 CRUD 接口 —— 行情看板「我的持仓」数据存 MySQL，按用户隔离。

接口（均需登录，Header 带 Authorization: Bearer <token>）：
  GET    /api/positions          读当前用户全部持仓
  POST   /api/positions          新增（同品种可建多条，不校验唯一性）
  PUT    /api/positions/{id}     修改（字段可选，只更新传入项）
  DELETE /api/positions/{id}     删除
"""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Position, User
from app.schemas import PositionCreate, PositionUpdate, PositionOut

router = APIRouter(tags=["positions"])


@router.get("/api/positions", response_model=list[PositionOut])
def list_positions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读当前用户全部持仓，按 id 升序。"""
    return db.scalars(
        select(Position).where(Position.user_id == user.id).order_by(Position.id)
    ).all()


@router.post("/api/positions", response_model=PositionOut, status_code=status.HTTP_201_CREATED)
def create_position(
    data: PositionCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新增持仓。允许同一品种建多条（如分批建仓），不校验 code 唯一性。"""
    pos = Position(
        user_id=user.id,
        code=data.code,
        direction=data.direction,
        buy_price=data.buy_price,
        lots=data.lots,
        multiplier=data.multiplier,
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


@router.put("/api/positions/{pos_id}", response_model=PositionOut)
def update_position(
    pos_id: int,
    data: PositionUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """修改持仓。仅更新显式传入的字段；不校验 code 唯一性（允许同品种多条）。"""
    pos = db.scalar(select(Position).where(Position.id == pos_id, Position.user_id == user.id))
    if pos is None:
        raise HTTPException(status_code=404, detail="持仓不存在")

    updates = data.model_dump(exclude_unset=True)

    for field, value in updates.items():
        setattr(pos, field, value)

    # 显式刷新更新时间：若传入的值与库中完全相同，SQLAlchemy 的脏检查判定
    # "无变化"会跳过 UPDATE，导致 onupdate=func.now() 不触发、时间戳不动。
    # 这里手动赋值后必定产生一次 UPDATE。用 func.now() 与 created_at 的
    # server_default 保持一致（同取 MySQL 的 NOW()，不混入 Python 时区）。
    pos.updated_at = func.now()

    db.commit()
    db.refresh(pos)
    return pos


@router.delete("/api/positions/{pos_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_position(
    pos_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除持仓（仅能删除本人持仓）。"""
    pos = db.scalar(select(Position).where(Position.id == pos_id, Position.user_id == user.id))
    if pos is None:
        raise HTTPException(status_code=404, detail="持仓不存在")
    db.delete(pos)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
