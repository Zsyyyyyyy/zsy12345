import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

load_dotenv()

# MySQL 连接串：mysql+pymysql://用户:密码@主机:端口/库名
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://root:123456@127.0.0.1:3306/fastapi_auth",
)

engine = create_engine(DATABASE_URL, echo=True, pool_pre_ping=True)
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
