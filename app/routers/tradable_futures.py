#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国内可交易品种字典路由 —— 行情看板「新增持仓」校验 + 前端下拉框。

接口：
  GET /api/tradable-futures               列出全部在交易品种（按 exchange、code 排序）
  GET /api/tradable-futures/{code}        查单个品种
  POST /api/tradable-futures/validate     校验 code 是否合法（批量），body={"codes":["nf_RB2701",...]}

提供模块级工具函数 `validate_position_code(code, db)` 给 positions.py 复用：
  - nf_ 前缀 → 解析 underlying + 月份 → 查表 + 校验月份
  - hf_/sz/sh/bj/hk 等其他 code → 直接通过（不在校验范围）
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import TradableFuture
from app.schemas import TradableFutureOut

router = APIRouter(tags=["tradable-futures"])


# ========== 公共工具（供 positions.py 复用） ==========

# 国内期货 code 形如 nf_<underlying><4位月份> 或 nf_<underlying>0
import re
_DOM_CODE_RE = re.compile(r'^nf_([A-Za-z]+)(\d{4}|0)$')

# 把 delivery_months 字符串（如 "1,5,10" / "1-12"）解析成 set
def _parse_months(spec: str) -> set[int]:
    out: set[int] = set()
    for part in (spec or '').split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def validate_position_code(code: str, db: Session) -> tuple[bool, str | None]:
    """校验持仓 code 是否合法。返回 (ok, 错误信息)。
    - 其它前缀（hf_/sh/sz/bj/hk）视为通过，让原校验逻辑继续处理
    - nf_ 但品种/月份不在表里，返回 (False, 错误信息)
    """
    m = _DOM_CODE_RE.match((code or '').strip())
    if not m:
        # 非 nf_ 前缀或格式错：不归本表管，调用方自己决定
        return True, None
    underlying, month_tag = m.group(1).upper(), m.group(2)
    tf = db.scalar(select(TradableFuture).where(TradableFuture.code == underlying))
    if tf is None or not tf.is_active:
        return False, f'品种 {underlying} 不在可交易清单中（或已下市）'
    # month_tag == "0" 表示连续合约，放行
    if month_tag == '0':
        return True, None
    # 4 位月份形如 "2701" = 2027年1月 → 取月份
    try:
        month = int(month_tag[2:])  # 2701 -> 01 -> 1
    except ValueError:
        return False, f'合约月份格式错误：{month_tag}'
    if month not in _parse_months(tf.delivery_months):
        return False, f'{tf.name}({tf.code}) 不在可交割月份（{tf.delivery_months}）中，当前 {month} 月'
    return True, None


def auto_fill_multiplier(code: str, db: Session) -> float | None:
    """若持仓未传 multiplier，且 code 是国内期货，则从品种表自动补乘数。"""
    m = _DOM_CODE_RE.match((code or '').strip())
    if not m:
        return None
    tf = db.scalar(select(TradableFuture).where(TradableFuture.code == m.group(1).upper()))
    return tf.multiplier if tf else None


# ========== HTTP 端点 ==========

@router.get("/api/tradable-futures", response_model=list[TradableFutureOut])
def list_tradable_futures(
    active_only: bool = True,
    db: Session = Depends(get_db),
):
    """列出全部在交易品种。前端拿去做下拉框或校验。"""
    stmt = select(TradableFuture).order_by(TradableFuture.exchange, TradableFuture.code)
    if active_only:
        stmt = stmt.where(TradableFuture.is_active.is_(True))
    return db.scalars(stmt).all()


@router.get("/api/tradable-futures/{code}", response_model=TradableFutureOut)
def get_tradable_future(code: str, db: Session = Depends(get_db)):
    """查单个品种。"""
    tf = db.scalar(select(TradableFuture).where(TradableFuture.code == code.upper()))
    if tf is None:
        raise HTTPException(status_code=404, detail="品种不存在")
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