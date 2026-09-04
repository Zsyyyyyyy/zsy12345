import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

load_dotenv()

# MySQL 连接串：mysql+pymysql://用户:密码@主机:端口/库名
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://root:123456@127.0.0.1:3306/zsy12345",
)

engine = create_engine(
    DATABASE_URL,
    echo=True,
    pool_pre_ping=True,
    # 强制每个连接使用北京时间（UTC+8），保证 func.now()/NOW() 落库时间与业务一致，
    # 不受服务器系统时区或 MySQL 全局 time_zone 影响。
    connect_args={"init_command": "SET time_zone = '+08:00'"},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类"""
    pass


def get_db():
    """FastAPI 依赖：每个请求拿一个 session，用完自动关闭"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
