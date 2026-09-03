#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓 CRUD 接口 —— 行情看板「我的持仓」数据存 MySQL，按用户隔离。

接口（均需登录，Header 带 Authorization: Bearer <token>）：
  GET    /api/positions          读当前用户全部持仓
  POST   /api/positions          新增（同一用户内 code 唯一，重复返回 409）
  PUT    /api/positions/{id}     修改（字段可选，只更新传入项）
  DELETE /api/positions/{id}     删除
"""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
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
    """新增持仓。同一用户内 code 已存在则返回 409。"""
    if db.scalar(select(Position).where(Position.user_id == user.id, Position.code == data.code)):
        raise HTTPException(status_code=409, detail="该品种已存在，请用「修改」编辑")
    pos = Position(
        user_id=user.id,
        code=data.code,
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
    """修改持仓。仅更新显式传入的字段；改 code 时在本人范围内查重（排除自身）。"""
    pos = db.scalar(select(Position).where(Position.id == pos_id, Position.user_id == user.id))
    if pos is None:
        raise HTTPException(status_code=404, detail="持仓不存在")

    updates = data.model_dump(exclude_unset=True)
    if updates.get("code") is not None:
        dup = db.scalar(select(Position).where(
            Position.user_id == user.id,
            Position.code == updates["code"],
            Position.id != pos_id,
        ))
        if dup:
            raise HTTPException(status_code=409, detail="该品种已存在")

    for field, value in updates.items():
        setattr(pos, field, value)

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
