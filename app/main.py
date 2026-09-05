from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.database import Base, engine
from app.routers.auth import router as auth_router
from app.routers.history import router as history_router
from app.routers.positions import router as positions_router
from app.routers.realtime import router as realtime_router
from app.routers.settlements import router as settlements_router
from app.routers.watchlist import router as watchlist_router

# 启动时自动建表（已手动建过则无副作用；生产环境建议换 Alembic 迁移）
Base.metadata.create_all(bind=engine)

app = FastAPI(title="FastAPI 登录注册示例")

# 项目根目录（本文件位于 app/ 下，上一级即项目根）
BASE_DIR = Path(__file__).resolve().parent.parent


@app.get("/")
def index():
    """首页（工具箱）"""
    return FileResponse(BASE_DIR / "index.html")


@app.get("/login")
def login_page():
    """登录 / 注册页"""
    return FileResponse(BASE_DIR / "login.html")


@app.get("/favicon.svg")
def favicon():
    """浏览器标签页小图标"""
    return FileResponse(BASE_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/futures")
def futures_page():
    """行情看板（前端展示，接口由本后端提供）"""
    return FileResponse(BASE_DIR / "modules" / "futures" / "public" / "dashboard.html")


# 认证接口：/register、/login、/me
app.include_router(auth_router)

# ① 实时行情接口（网页抓取新浪）：/api/futures、suggest、minline、dailykline
app.include_router(realtime_router)

# ② 历史行情 + 品种信息接口（读数据库，数据由脚本 refresh_tradable_futures.py 定时刷新）：
#    /api/futures-base*、/api/futures/hist-position、/api/history/dailybars
app.include_router(history_router)

# 持仓 CRUD 接口：/api/positions
app.include_router(positions_router)

# 结算接口：/api/positions/{id}/settle、/api/settlements
app.include_router(settlements_router)

# 看盘分组 CRUD 接口：/api/groups
app.include_router(watchlist_router)

# 显式挂载各静态工具子目录（排除 futures：其前端由 /futures 路由提供，避免暴露源码/配置）
_MODULE_DIRS = ["hash", "currency-converter", "fortune", "timestamp", "tetris", "zhconvert", "notes", "price-calc", "lib"]
for _name in _MODULE_DIRS:
    app.mount(f"/modules/{_name}", StaticFiles(directory=BASE_DIR / "modules" / _name), name=f"modules_{_name}")
