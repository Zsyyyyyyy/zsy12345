from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import hash_password, verify_password, create_access_token, get_current_user
from app.models import User
from app.schemas import UserRegister, UserLogin, Token, UserOut

router = APIRouter(tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
def register(data: UserRegister, db: Session = Depends(get_db)):
    # 1. 查重：用户名或邮箱已存在则报错
    exists = db.scalar(select(User).where(
        (User.username == data.username) | (User.email == data.email)
    ))
    if exists:
        raise HTTPException(status_code=400, detail="用户名或邮箱已被注册")

    # 2. 密码哈希后落库
    user = User(username=data.username, email=data.email,
                password_hash=hash_password(data.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=Token)
def login(data: UserLogin, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == data.username))

    # 用户不存在 / 密码错误统一返回 401，不暴露具体是哪一项
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    token = create_access_token(user.id, user.username)
    return Token(access_token=token)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    """受保护接口：需在请求头带 Authorization: Bearer <token>"""
    return user
