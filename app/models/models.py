from datetime import datetime
from sqlalchemy import BigInteger, String, Boolean, DateTime, Float, Integer, JSON, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, comment="用户名")
    email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, comment="邮箱")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, comment="密码哈希")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )


class TradableFuture(Base):
    """国内可交易期货品种字典（全局共享，按品种 underlying 主键）。

    持仓接口用此表校验新增持仓：code 形如 `nf_<underlying><4位月份>`
    或 `nf_<underlying>0`（连续合约），要求 underlying 在表里 + 月份在
    delivery_months 列表里。海外期货/股票/港股不走此校验。
    """
    __tablename__ = "tradable_futures"

    code: Mapped[str] = mapped_column(String(16), primary_key=True, comment="品种 underlying（如 RB/CU/SA）")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="中文名（螺纹钢/铜/纯碱）")
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, comment="交易所 SHFE/DCE/CZCE/GFEX/INE")
    multiplier: Mapped[float] = mapped_column(Float, nullable=False, comment="合约乘数（每点价值，元）")
    tick_size: Mapped[float] = mapped_column(Float, nullable=False, default=1.0, comment="最小变动价位")
    delivery_months: Mapped[str] = mapped_column(
        String(48), nullable=False, default="",
        comment="可交割月份，逗号分隔（如 '1,5,10' 或 '1-12'）",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否仍在交易（下市品种置 False 但保留历史）")
    note: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="备注（如上市/下市日期）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )


class Position(Base):
    """期货/股票持仓（行情看板「我的持仓」，按用户隔离；同一品种可建多条）"""
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="所属用户 id")
    code: Mapped[str] = mapped_column(String(32), nullable=False, comment="品种代码（如 nf_SA2701）")
    direction: Mapped[str] = mapped_column(
        String(8), default="long", server_default="long", nullable=False,
        comment="方向：long=做多 / short=做空",
    )
    buy_price: Mapped[float] = mapped_column(Float, nullable=False, comment="开仓价（做多=买入价，做空=卖出价）")
    lots: Mapped[float] = mapped_column(Float, default=1.0, nullable=False, comment="手数")
    multiplier: Mapped[float | None] = mapped_column(Float, nullable=True, comment="每点价值（可选，不填则按品种自动查）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )


class WatchGroup(Base):
    """看盘分组（行情看板左侧看盘清单，按用户隔离）"""
    __tablename__ = "watch_groups"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_watch_groups_user_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="所属用户 id")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="分组名")
    codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="品种代码数组（JSON）")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="排序序号（越小越靠前）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )


class Settlement(Base):
    """结算记录（持仓平仓时的成交快照与盈亏，按用户隔离）"""
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="所属用户 id")
    code: Mapped[str] = mapped_column(String(32), nullable=False, comment="品种代码（如 nf_SA2701）")
    direction: Mapped[str] = mapped_column(
        String(8), default="long", server_default="long", nullable=False,
        comment="开仓方向快照：long=做多 / short=做空",
    )
    name: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="品种名称快照")
    buy_price: Mapped[float] = mapped_column(Float, nullable=False, comment="开仓价快照")
    settle_price: Mapped[float] = mapped_column(Float, nullable=False, comment="结算价")
    spread: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False,
        comment="按方向价差（做多=结算价-开仓价，做空=开仓价-结算价），便于历史表直接显示",
    )
    lots: Mapped[float] = mapped_column(Float, default=1.0, nullable=False, comment="手数")
    multiplier: Mapped[float | None] = mapped_column(Float, nullable=True, comment="每点价值快照")
    pnl: Mapped[float] = mapped_column(Float, nullable=False, comment="盈亏（已按方向计：多=(结算价-开仓价)，空=(开仓价-结算价)，再×手数×乘数）")
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False, comment="币种 CNY/USD/HKD")
    settled_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="结算时间")
