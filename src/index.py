"""
服务入口 — FastAPI 应用装配与启动。

支持 HTTP / HTTPS（可选自签证书）。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config, ensure_dirs, Config
from .logger import init_logger, get_logger
from .store import init_store, task_logs_store, sessions_store, config_store, sanitize_linkid
from .services.task_queue import init_task_queue, start_recovered_pump
from .routes.health import router as health_router
from .routes.auth import router as auth_router, build_api_token_dependency
from .routes.process import create_process_router
from .routes.tasks import create_tasks_router
from .routes.admin import create_admin_router

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _mask(s: str) -> str:
    if not s:
        return "(empty)"
    if len(s) <= 8:
        return "***"
    return s[:4] + "***" + s[-4:]


def create_app() -> tuple[FastAPI, Config]:
    """构建 FastAPI 应用并初始化依赖。"""
    cfg = load_config()
    ensure_dirs(cfg)

    log = init_logger(cfg.sys.data_dir, cfg.server.debug)
    log.info("==== dp_agt_py starting ====")
    log.info(
        "config_loaded server=%s sys=%s llm=%s doc_audit=%s",
        cfg.server.__dict__,
        {
            **cfg.sys.__dict__,
            "api_token": _mask(cfg.sys.api_token),
            "file_url_token": _mask(cfg.sys.file_url_token),
        },
        {**cfg.llm.__dict__, "auth_token": _mask(cfg.llm.auth_token)},
        {**cfg.doc_audit.__dict__, "token": _mask(cfg.doc_audit.token)},
    )

    # 初始化存储
    init_store(cfg.sys.data_dir)
    log.info("store_ready")

    # 初始化任务队列
    init_task_queue(cfg)
    log.info("task_queue_ready")

    app = FastAPI(title="dp_agt_py 管理后台", docs_url=None, redoc_url=None)

    # ============================================================
    # 公开路由（无需认证）
    # ============================================================
    app.include_router(health_router)
    app.include_router(auth_router)

    # 登录页面
    @app.get("/login", include_in_schema=False)
    async def login_page():
        return FileResponse(str(_WEB_DIR / "login.html"))

    # 公开系统信息（登录页用，无需认证）
    @app.get("/api/info")
    async def api_info():
        name = config_store.get("sys.name") or "dp_agt_py"
        return {"code": 0, "data": {"sys_name": name}}

    # 静态资源（公开，登录页也需要）
    if (_WEB_DIR / "static").exists():
        app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    # ============================================================
    # 对外业务接口（外部系统调用，Bearer/API-Key 鉴权）
    # ============================================================
    app.include_router(create_process_router(cfg))
    app.include_router(create_tasks_router(cfg))

    # Dev null — 用于调试接收请求
    @app.post("/api/dev/null")
    async def dev_null(request: Request):
        body = await request.json()
        log.info("dev_null_received body=%s", body)
        return {"status": "ok"}

    # ============================================================
    # 管理后台 API（需要登录，内部有 admin 权限区分）
    # ============================================================
    app.include_router(create_admin_router(cfg))

    # 管理后台 Web 页面
    @app.get("/admin", include_in_schema=False)
    async def admin_page():
        return FileResponse(str(_WEB_DIR / "admin.html"))

    # 根路径重定向
    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/admin")

    return app, cfg


app, cfg = create_app()
log = get_logger()


async def _cleanup_expired_tasks_loop():
    """定期清理过期任务数据。"""
    retention_days = cfg.sys.retention_days
    # 启动后 10 秒清理一次，之后每 24 小时清理一次
    await asyncio.sleep(10)
    while True:
        try:
            deleted = task_logs_store.delete_expired(retention_days)

            # 清理过期 session（超过 retentionDays 未见新提交）
            expired_sessions = sessions_store.delete_expired(retention_days)
            if expired_sessions:
                log.info("session_cleanup_start expired_count=%d retention_days=%d", len(expired_sessions), retention_days)
                for s in expired_sessions:
                    try:
                        from claude_agent_sdk import delete_session
                        await delete_session(s.session_id)
                    except Exception as err:
                        log.warning("session_delete_sdk_error session_id=%s error=%s", s.session_id, err)

            # 清理过期 session 对应的磁盘工作目录
            tasks_dir = Path(cfg.sys.data_dir) / "tasks"
            if tasks_dir.exists():
                active_set = {sanitize_linkid(s.linkid) for s in sessions_store.list_all()}
                for d in tasks_dir.iterdir():
                    if d.name not in active_set:
                        shutil.rmtree(d, ignore_errors=True)

            log.info("cleanup_done retention_days=%d deleted_rows=%d expired_sessions=%d", retention_days, deleted, len(expired_sessions))
        except Exception as err:
            log.error("cleanup_error error=%s", err)
        await asyncio.sleep(24 * 60 * 60)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_cleanup_expired_tasks_loop())
    start_recovered_pump()
    log.info(
        "cleanup_scheduled retention_days=%d interval_ms=%d",
        cfg.sys.retention_days, 24 * 60 * 60 * 1000,
    )


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

    log.info(
        "binding_port port=%d cert_file=%s", port, cert_file or "(none)",
    )
    log.info("server_started_%s port=%d url=%s://localhost:%d", proto, port, proto, port)

    uvicorn.run(app, host="0.0.0.0", port=port, **ssl_kwargs)


if __name__ == "__main__":
    main()