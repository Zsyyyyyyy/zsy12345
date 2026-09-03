from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field


class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)


class UserLogin(BaseModel):
    username: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True  # 让 ORM 对象能直接转成这个模型


# ===== 持仓 =====
class PositionCreate(BaseModel):
    """新增持仓"""
    code: str = Field(..., min_length=2, max_length=32)
    buy_price: float
    lots: float = 1.0
    multiplier: Optional[float] = None


class PositionUpdate(BaseModel):
    """修改持仓（字段均可选，只更新传入的字段）"""
    code: Optional[str] = Field(None, min_length=2, max_length=32)
    buy_price: Optional[float] = None
    lots: Optional[float] = None
    multiplier: Optional[float] = None


class PositionOut(BaseModel):
    id: int
    code: str
    buy_price: float
    lots: float
    multiplier: Optional[float]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ===== 结算 =====
class SettlementCreate(BaseModel):
    """结算持仓：只需结算价，其余字段（名称/币种/乘数）可选，后端会从持仓快照兜底"""
    settle_price: float
    name: Optional[str] = None
    currency: Optional[str] = None
    multiplier: Optional[float] = None


class SettlementOut(BaseModel):
    id: int
    code: str
    name: Optional[str]
    buy_price: float
    settle_price: float
    lots: float
    multiplier: Optional[float]
    pnl: float
    currency: str
    settled_at: datetime

    class Config:
        from_attributes = True


# ===== 看盘分组 =====
class WatchGroupCreate(BaseModel):
    """新增看盘分组"""
    name: str = Field(..., min_length=1, max_length=50)
    codes: list[str] = []
    sort_order: int = 0


class WatchGroupUpdate(BaseModel):
    """修改看盘分组（字段均可选，只更新传入的字段）"""
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    codes: Optional[list[str]] = None
    sort_order: Optional[int] = None


class WatchGroupOut(BaseModel):
    id: int
    name: str
    codes: list[str]
    sort_order: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
