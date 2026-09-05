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

# 首次登录播种用的默认分组（与前端 dashboard.html 的 DEFAULT_GROUPS 保持一致）
DEFAULT_GROUPS = [
    {"name": "股票", "codes": ["sh600519", "sh600036", "sh601318"]},
    {"name": "指数", "codes": ["sh000905", "sh000688", "bj899050", "hkHSTECH", "sh000001", "sz399001"]},
    {"name": "国内商品期货", "codes": ["nf_RB0", "nf_CU0", "nf_AU0", "nf_AG0", "nf_I0", "nf_M0", "nf_MA0", "nf_SC0", "nf_TA0", "nf_P0"]},
    {"name": "海外期货", "codes": ["hf_OIL", "hf_CL", "hf_GC", "hf_SI", "hf_NG", "hf_HG"]},
]


@router.get("/api/groups", response_model=list[WatchGroupOut])
def list_groups(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读当前用户全部分组，按 sort_order、id 升序。

    首次使用（名下一个分组都没有）时，把默认分组播种到该用户名下，
    保证登录后看到的默认品种可以直接增删改。
    """
    groups = db.scalars(
        select(WatchGroup).where(WatchGroup.user_id == user.id)
        .order_by(WatchGroup.sort_order, WatchGroup.id)
    ).all()
    if not groups:
        groups = []
        for i, d in enumerate(DEFAULT_GROUPS):
            g = WatchGroup(user_id=user.id, name=d["name"], codes=d["codes"], sort_order=i)
            db.add(g)
            groups.append(g)
        db.commit()
        for g in groups:
            db.refresh(g)
    return groups


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
