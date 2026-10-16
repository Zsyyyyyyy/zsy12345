from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.database import Base, engine
from app.routers.auth import router as auth_router

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


# 认证接口：/register、/login、/me
app.include_router(auth_router)

# 自动发现 modules/ 下的静态工具子目录并挂载，约定：目录名即模块名，入口为 <模块名>.html
# 新增工具：在 modules/ 下建目录 + 放同名 .html，重启服务即生效，无需改这里。
# 下划线开头的目录跳过。
_MODULES_DIR = BASE_DIR / "modules"
for _mod_dir in sorted(_MODULES_DIR.iterdir()):
    if not _mod_dir.is_dir() or _mod_dir.name.startswith("_"):
        continue
    app.mount(f"/modules/{_mod_dir.name}", StaticFiles(directory=_mod_dir), name=f"modules_{_mod_dir.name}")
