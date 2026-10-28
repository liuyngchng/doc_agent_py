"""
服务入口 — 对应 TypeScript 版 `index.ts`。

FastAPI + HTTPS 支持（可选自签证书）。
"""

from __future__ import annotations

import ssl
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config, ensure_dirs
from .logger import init_logger
from .store import init_store
from .services.task_queue import init_task_queue
from .routes.health import router as health_router
from .routes.auth import router as auth_router
from .routes.process import create_process_router
from .routes.tasks import router as tasks_router
from .routes.admin import router as admin_router
from .routes.auth import get_current_user


def create_app() -> FastAPI:
    cfg = load_config()
    ensure_dirs(cfg)

    log = init_logger(cfg.sys.data_dir, cfg.server.debug)
    log.info("starting config=%s", cfg.server.__dict__)

    # 初始化存储
    init_store(cfg.sys.data_dir)

    # 初始化任务队列
    init_task_queue(cfg)
    log.info("task_queue_ready")

    app = FastAPI(title="doc_process_agent 管理后台", docs_url=None, redoc_url=None)

    # 公开路由
    app.include_router(health_router)
    app.include_router(auth_router)

    # 登录页面
    @app.get("/login", include_in_schema=False)
    async def login_page():
        return FileResponse("web/login.html")

    # 静态资源
    web_dir = Path(__file__).resolve().parent.parent / "web"
    if (web_dir / "static").exists():
        app.mount("/static", StaticFiles(directory=str(web_dir / "static")), name="static")

    # 对外业务接口
    app.include_router(create_process_router(cfg))

    # 任务管理 API
    app.include_router(tasks_router)

    # Dev null
    @app.post("/api/dev/null")
    async def dev_null(request: Request):
        body = await request.json()
        log.info("dev_null_received body=%s", body)
        return {"status": "ok"}

    # 管理后台 API（需要登录）
    app.include_router(admin_router)

    # 管理后台 Web 页面
    @app.get("/admin", include_in_schema=False)
    async def admin_page():
        return FileResponse("web/admin.html")

    # 根路径重定向
    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/admin")

    return app, cfg


app, cfg = create_app()


def main() -> None:
    """启动服务。"""
    port = cfg.server.port
    cert_file = cfg.server.cert_file
    key_file = cfg.server.key_file

    ssl_kwargs = {}
    if cert_file and key_file and Path(cert_file).exists() and Path(key_file).exists():
        ssl_kwargs = {
            "ssl_certfile": cert_file,
            "ssl_keyfile": key_file,
        }
        proto = "https"
    else:
        proto = "http"

    log.info("server_started_%s port=%d url=%s://localhost:%d", proto, port, proto, port)

    uvicorn.run(app, host="0.0.0.0", port=port, **ssl_kwargs)


if __name__ == "__main__":
    main()
