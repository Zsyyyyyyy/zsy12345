#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看盘分组 CRUD 接口 —— 行情看板左侧「看盘清单」存 MySQL，按用户隔离。

接口（均需登录，Header 带 Authorization: Bearer <token>）：
  GET    /api/groups           读当前用户全部分组（按 sort_order、id 升序）
  POST   /api/groups           新增分组（同一用户内 name 唯一，重复返回 409）
  PUT    /api/groups/{id}      修改分组（字段可选，只更新传入项）
  DELETE /api/groups/{id}      删除分组
"""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import WatchGroup, User
from app.schemas import WatchGroupCreate, WatchGroupUpdate, WatchGroupOut

router = APIRouter(tags=["watchlist"])


@router.get("/api/groups", response_model=list[WatchGroupOut])
def list_groups(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读当前用户全部分组，按 sort_order、id 升序。"""
    return db.scalars(
        select(WatchGroup).where(WatchGroup.user_id == user.id)
        .order_by(WatchGroup.sort_order, WatchGroup.id)
    ).all()


@router.post("/api/groups", response_model=WatchGroupOut, status_code=status.HTTP_201_CREATED)
def create_group(
    data: WatchGroupCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新增分组。同一用户内 name 已存在则返回 409。"""
    if db.scalar(select(WatchGroup).where(WatchGroup.user_id == user.id, WatchGroup.name == data.name)):
        raise HTTPException(status_code=409, detail="该分组名已存在")
    g = WatchGroup(user_id=user.id, name=data.name, codes=data.codes, sort_order=data.sort_order)
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


@router.put("/api/groups/{group_id}", response_model=WatchGroupOut)
def update_group(
    group_id: int,
    data: WatchGroupUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """修改分组。仅更新显式传入的字段；改 name 时在本人范围内查重（排除自身）。"""
    g = db.scalar(select(WatchGroup).where(WatchGroup.id == group_id, WatchGroup.user_id == user.id))
    if g is None:
        raise HTTPException(status_code=404, detail="分组不存在")

    updates = data.model_dump(exclude_unset=True)
    if updates.get("name") is not None:
        dup = db.scalar(select(WatchGroup).where(
            WatchGroup.user_id == user.id,
            WatchGroup.name == updates["name"],
            WatchGroup.id != group_id,
        ))
        if dup:
            raise HTTPException(status_code=409, detail="该分组名已存在")

    for field, value in updates.items():
        setattr(g, field, value)

    db.commit()
    db.refresh(g)
    return g


@router.delete("/api/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除分组（仅能删除本人分组）。"""
    g = db.scalar(select(WatchGroup).where(WatchGroup.id == group_id, WatchGroup.user_id == user.id))
    if g is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    db.delete(g)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
