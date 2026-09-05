#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看盘分组 CRUD 接口 —— 行情看板左侧「看盘清单」存 MySQL，按用户隔离。

范围仅限「国内期货(nf_) + A股证券/指数(sh/sz/bj)」：海外期货(hf_)、
港股(hk) 已下线，接口在读写分组时自动把这些代码从 codes 里剔除
（库中旧数据保留，读出即过滤；再保存时写回的是过滤后的列表）。

接口（均需登录，Header 带 Authorization: Bearer <token>）：
  GET    /api/groups           读当前用户全部分组（按 sort_order、id 升序）
  POST   /api/groups           新增分组（同一用户内 name 唯一，重复返回 409）
  PUT    /api/groups/{id}      修改分组（字段可选，只更新传入项）
  DELETE /api/groups/{id}      删除分组
"""
import re

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import WatchGroup, User
from app.schemas import WatchGroupCreate, WatchGroupUpdate, WatchGroupOut

router = APIRouter(tags=["watchlist"])

# A股证券/指数代码：sh600519 / sz000001 / bj899050
_A_SHARE_RE = re.compile(r'^(sh|sz|bj)\d{6}$')


def _is_supported_code(code: str) -> bool:
    """看盘分组仅支持：国内期货 nf_ + A股证券/指数 sh/sz/bj。"""
    c = str(code or '').strip().lower()
    return c.startswith('nf_') or bool(_A_SHARE_RE.match(c))


def _filter_codes(codes) -> list[str]:
    """剔除 hf_/hk 等已下线代码，并去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for c in codes or []:
        s = str(c).strip()
        if not s or s in seen or not _is_supported_code(s):
            continue
        seen.add(s)
        out.append(s)
    return out


def _sanitize_group(g: WatchGroup) -> WatchGroup:
    """返回过滤掉下线代码后的分组对象（不落库，仅作用于本次响应）。"""
    g.codes = _filter_codes(g.codes)
    return g


@router.get("/api/groups", response_model=list[WatchGroupOut])
def list_groups(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读当前用户全部分组，按 sort_order、id 升序（hf_/hk 等已下线代码自动剔除）。"""
    groups = db.scalars(
        select(WatchGroup).where(WatchGroup.user_id == user.id)
        .order_by(WatchGroup.sort_order, WatchGroup.id)
    ).all()
    return [_sanitize_group(g) for g in groups]


@router.post("/api/groups", response_model=WatchGroupOut, status_code=status.HTTP_201_CREATED)
def create_group(
    data: WatchGroupCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新增分组。同一用户内 name 已存在则返回 409；codes 自动过滤下线代码。"""
    if db.scalar(select(WatchGroup).where(WatchGroup.user_id == user.id, WatchGroup.name == data.name)):
        raise HTTPException(status_code=409, detail="该分组名已存在")
    g = WatchGroup(
        user_id=user.id,
        name=data.name,
        codes=_filter_codes(data.codes),
        sort_order=data.sort_order,
    )
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
    """修改分组。仅更新显式传入的字段；codes 更新时自动过滤下线代码。"""
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

    if updates.get("codes") is not None:
        updates["codes"] = _filter_codes(updates["codes"])

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
