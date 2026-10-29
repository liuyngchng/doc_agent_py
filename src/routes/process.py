"""
对外业务接口路由 — 对应 TypeScript 版 `routes/process.ts`。

POST /api/v1/{path:path} — 通配路由。
"""

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

from ..config import Config
from ..logger import get_logger
from ..store import interfaces_store
from ..services.task_queue import enqueue, get_running_count, get_pending_count
from .auth import build_api_token_dependency

log = get_logger()


def _build_response(code: int, message: str, task_id: str, linkid: str) -> dict:
    from datetime import datetime, timezone
    return {
        "code": code,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("T", " ")[:19],
        "message": message,
        "task_id": task_id,
        "linkid": linkid,
        "data": {},
    }


def create_process_router(cfg: Config) -> APIRouter:
    router = APIRouter()

    @router.api_route("/api/v1/{rest_of_path:path}", methods=["POST"])
    async def process(
        rest_of_path: str,
        request: Request,
        _auth: None = Depends(build_api_token_dependency(cfg.sys.api_token)),
    ):
        interface_path = rest_of_path
        if not interface_path:
            return JSONResponse(
                status_code=404,
                content=_build_response(1001, "缺少接口路径", "", ""),
            )

        # 查找接口定义
        iface = interfaces_store.get_by_path(interface_path)
        if not iface:
            return JSONResponse(
                status_code=404,
                content=_build_response(1001, f"接口不存在: {interface_path}", "", ""),
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content=_build_response(1001, "请求体不是有效的 JSON", "", ""),
            )

        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content=_build_response(1001, "请求体不能为空", "", ""),
            )
        if not body.get("linkid"):
            return JSONResponse(
                status_code=400,
                content=_build_response(1001, "缺少 linkid", "", ""),
            )
        if not body.get("callback_url"):
            return JSONResponse(
                status_code=400,
                content=_build_response(1001, "缺少 callback_url", "", ""),
            )

        # 检查队列上限
        max_pending = cfg.sys.max_pending_tasks
        active_count = get_running_count() + get_pending_count()
        if active_count >= max_pending:
            msg = (
                f"任务队列已满，当前处理中任务数 {active_count}/{max_pending}"
                f"（最大并发 {cfg.sys.max_concurrent_tasks}，最大排队 {max_pending}），请稍后重试"
            )
            return JSONResponse(
                status_code=503,
                content=_build_response(2002, msg, "", body.get("linkid", "")),
            )

        # 入队
        result = enqueue(iface, body)
        task_id = result["task_id"]

        log.info("task_enqueued task_id=%s linkid=%s path=%s", task_id, body.get("linkid"), interface_path)
        return _build_response(0, "success", task_id, body.get("linkid", ""))

    return router
