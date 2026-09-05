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


# 方向取值：long=做多 / short=做空
DIRECTION_PATTERN = r"^(long|short)$"


# ===== 持仓 =====
class PositionCreate(BaseModel):
    """新增持仓"""
    code: str = Field(..., min_length=2, max_length=32)
    direction: str = Field("long", pattern=DIRECTION_PATTERN, description="long=做多 / short=做空")
    buy_price: float
    lots: float = 1.0
    multiplier: Optional[float] = None


class PositionUpdate(BaseModel):
    """修改持仓（字段均可选，只更新传入的字段）"""
    code: Optional[str] = Field(None, min_length=2, max_length=32)
    direction: Optional[str] = Field(None, pattern=DIRECTION_PATTERN, description="long=做多 / short=做空")
    buy_price: Optional[float] = None
    lots: Optional[float] = None
    multiplier: Optional[float] = None


class PositionOut(BaseModel):
    id: int
    code: str
    direction: str
    buy_price: float
    lots: float
    multiplier: Optional[float]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ===== 结算 =====
class SettlementCreate(BaseModel):
    """结算持仓：只需结算价，其余字段（名称/币种/乘数/手数）可选，后端会从持仓快照兜底。
    手数 `lots` 不传或传 None 视为按该持仓剩余手数全部结算；传值需满足 0 < lots <= 持仓手数。
    """
    settle_price: float
    direction: Optional[str] = Field(None, pattern=DIRECTION_PATTERN, description="可选，缺省用持仓的方向")
    name: Optional[str] = None
    currency: Optional[str] = None
    multiplier: Optional[float] = None
    lots: Optional[float] = Field(None, gt=0, description="本次结算手数；缺省=全部")


class SettlementOut(BaseModel):
    id: int
    code: str
    direction: str
    name: Optional[str]
    buy_price: float
    settle_price: float
    spread: float
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


# ===== 期货「合约库」条目（当前挂牌 + 历史到期；是否可交易由前端按交割月判断）=====
class FuturesBaseOut(BaseModel):
    """期货合约库条目（给前端下拉框/联想用，无 is_active 状态位）"""
    code: str                 # 完整合约代码 nf_RB2701
    symbol: str               # 不带前缀 RB2701
    name: str                 # 合约中文名 螺纹钢2701
    underlying: str           # 品种代码 RB
    underlying_name: str      # 品种中文名 螺纹钢
    exchange: str             # SHFE/DCE/CZCE/CFFEX/GFEX
    multiplier: Optional[float]   # 每点价值
    tick_size: Optional[float]    # 最小变动价位

    class Config:
        from_attributes = True
