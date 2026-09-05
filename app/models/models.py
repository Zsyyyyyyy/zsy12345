from datetime import date, datetime
from sqlalchemy import BigInteger, String, Boolean, Date, DateTime, Float, Integer, JSON, UniqueConstraint, func
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


class FuturesBase(Base):
    """国内期货「合约库」：当前挂牌真实合约 + 已退市历史合约（全局共享，按完整合约主键）。

    表内同时存两类：
    - is_active=1：当前挂牌可交易的真实合约（如 nf_RB2701），
      由每日刷新接口（app/routers/data.py refresh_contracts / refresh_tradable_futures.py）
      负责「新增挂牌合约 + 把本次没再出现、不能交易的合约置 is_active=0」。
    - is_active=0：已退市合约（含 build_futures_base_history.py 从新浪逐月探测
      补进来的近约 5~7 年历史合约），保留用于历史持仓校验与历史行情对照。

    持仓接口据此校验：code 以 nf_ 开头时必须命中 is_active 合约；
    海外期货/股票/港股不走此校验。fetch_daily_history.py 默认抓取本表全部具体合约
    （含已下架）的日级历史行情。

    字段说明：
    - code：带前缀完整合约代码（如 nf_RB2701），与 positions.code 一致，作主键
    - symbol：不带前缀（如 RB2701），方便展示与前端联想
    - underlying / underlying_name：品种代码（RB）/ 品种中文名（螺纹钢），用于分组
    - multiplier / tick_size：品种级属性（每点价值/最小变动），刷新时按品种字典填充
    """
    __tablename__ = "futures_base"

    code: Mapped[str] = mapped_column(String(20), primary_key=True, comment="完整合约代码（如 nf_RB2701）")
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True, comment="不带前缀合约代码（如 RB2701）")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="合约中文名（如 螺纹钢2701）")
    underlying: Mapped[str] = mapped_column(String(8), nullable=False, index=True, comment="品种代码（如 RB）")
    underlying_name: Mapped[str] = mapped_column(String(50), nullable=False, default="", comment="品种中文名（如 螺纹钢）")
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, comment="交易所 SHFE/DCE/CZCE/CFFEX/GFEX")
    multiplier: Mapped[float | None] = mapped_column(Float, nullable=True, comment="合约乘数（每点价值，元；新品种可能为空）")
    tick_size: Mapped[float | None] = mapped_column(Float, nullable=True, comment="最小变动价位")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True, comment="是否仍在交易（到期/下架合约置 False 保留历史）")
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


class FuturesDailyBar(Base):
    """国内期货日级历史行情（新浪日K，由 fetch_daily_history.py 拉取）。

    - symbol：新浪合约代码（具体合约如 RB2701），不带 nf_ 前缀
    - 每行 = 某合约某个交易日的 OHLCV；contract_month 为该合约所属交割月份
    - (symbol, trade_date) 唯一，重复抓取按此键 upsert（幂等，可增量补数据）
    """
    __tablename__ = "futures_daily_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_futures_daily_bars_symbol_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True, comment="新浪合约代码（RB0 / RB2701）")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True, comment="交易日")
    contract_month: Mapped[date | None] = mapped_column(Date, nullable=True, index=True, comment="所属交割月份（取当月第一天，如 RB2701 → 2027-01-01；连续合约/老式3位代码为 NULL）")
    open: Mapped[float] = mapped_column("open_price", Float, nullable=True, comment="开盘价")
    high: Mapped[float] = mapped_column(Float, nullable=True, comment="最高价")
    low: Mapped[float] = mapped_column(Float, nullable=True, comment="最低价")
    close: Mapped[float] = mapped_column(Float, nullable=True, comment="收盘价")
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="成交量（手）")
    open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="持仓量（手，新浪部分品种缺省）")
    settlement: Mapped[float | None] = mapped_column(Float, nullable=True, comment="结算价（新浪 s 字段，早期数据可能为 0/缺失）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )
