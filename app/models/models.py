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


class Position(Base):
    """期货/股票持仓（行情看板「我的持仓」，按用户隔离）"""
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("user_id", "code", name="uq_positions_user_code"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="所属用户 id")
    code: Mapped[str] = mapped_column(String(32), nullable=False, comment="品种代码（如 nf_SA2701）")
    buy_price: Mapped[float] = mapped_column(Float, nullable=False, comment="买入价")
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
