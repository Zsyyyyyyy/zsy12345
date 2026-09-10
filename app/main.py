from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.database import Base, engine
from app.routers.auth import router as auth_router
from app.routers.futures_catalog_router import router as futures_catalog_router
from app.routers.futures_history_router import router as futures_history_router
from app.routers.positions import router as positions_router
from app.routers.quotes_router import router as quotes_router
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
app.include_router(quotes_router)

# ② 历史行情接口（读数据库，缺失日K时通过新浪客户端按需回填）：
#    /api/futures/hist-position、/api/history/dailybars
app.include_router(futures_history_router)

# ③ 期货合约目录接口（数据由 refresh_futures_base.py 定时刷新）
app.include_router(futures_catalog_router)

# 持仓 CRUD 接口：/api/positions
app.include_router(positions_router)

# 结算接口：/api/positions/{id}/settle、/api/settlements
app.include_router(settlements_router)

# 看盘分组 CRUD 接口：/api/groups
app.include_router(watchlist_router)

# 自动发现 modules/ 下的静态工具子目录并挂载，约定：目录名即模块名，入口为 <模块名>.html
# 新增工具：在 modules/ 下建目录 + 放同名 .html，重启服务即生效，无需改这里。
# 排除项：futures 走独立 /futures 路由（避免暴露 README 等非前端文件）；下划线开头的目录跳过。
_MODULE_EXCLUDES = {"futures"}
_MODULES_DIR = BASE_DIR / "modules"
for _mod_dir in sorted(_MODULES_DIR.iterdir()):
    if not _mod_dir.is_dir() or _mod_dir.name.startswith("_") or _mod_dir.name in _MODULE_EXCLUDES:
        continue
    app.mount(f"/modules/{_mod_dir.name}", StaticFiles(directory=_mod_dir), name=f"modules_{_mod_dir.name}")
