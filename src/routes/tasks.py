"""
任务管理路由 — 对应 TypeScript 版 `routes/tasks.ts`。

GET  /api/v1/tasks              — 查询进行中任务
DELETE /api/v1/tasks/{task_id}   — 取消任务
GET  /api/v1/task/trace         — 任务追踪
"""

import json
from fastapi import APIRouter, Query, HTTPException, Depends
from fastapi.responses import JSONResponse

from ..config import Config
from ..store import task_logs_store
from .auth import build_api_token_dependency


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("T", " ")[:19]


def _safe_parse(s: str | None) -> object:
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


def create_tasks_router(cfg: Config) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/tasks")
    async def list_tasks(
        _auth: None = Depends(build_api_token_dependency(cfg.sys.api_token)),
    ):
        tasks = task_logs_store.list_active()
        return {
            "code": 0,
            "timestamp": _now(),
            "message": "success",
            "task_id": "",
            "linkid": "",
            "data": {
                "total": len(tasks),
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "linkid": t.linkid,
                        "interface_path": t.path,
                        "status": t.status,
                        "callback_url": t.callback_url,
                        "created_at": t.created_at,
                    }
                    for t in tasks
                ],
            },
        }

    @router.delete("/api/v1/tasks/{task_id}")
    async def cancel_task(
        task_id: str,
        _auth: None = Depends(build_api_token_dependency(cfg.sys.api_token)),
    ):
        task = task_logs_store.get(task_id)

        if not task:
            return JSONResponse(
                status_code=404,
                content={"code": 1001, "message": "任务不存在或已结束", "task_id": "", "linkid": ""},
            )
        if task.status != "pending":
            return {
                "code": 1001,
                "message": "任务正在处理中，无法取消",
                "task_id": task_id,
                "linkid": task.linkid or "",
            }

        task_logs_store.update_status(task_id, "cancelled")  # type: ignore[arg-type]
        return {"code": 0, "message": "任务已取消", "task_id": task_id, "linkid": task.linkid or ""}

    @router.get("/api/v1/task/trace")
    async def task_trace(
        taskid: str = Query(default=""),
        _auth: None = Depends(build_api_token_dependency(cfg.sys.api_token)),
    ):
        if not taskid:
            return JSONResponse(
                status_code=400,
                content={"code": 1001, "message": "缺少 taskid 参数", "task_id": "", "linkid": ""},
            )

        task = task_logs_store.get(taskid)
        if not task:
            return JSONResponse(
                status_code=404,
                content={"code": 1001, "message": "任务不存在", "task_id": "", "linkid": ""},
            )

        return {
            "code": 0,
            "timestamp": _now(),
            "message": "success",
            "task_id": task.task_id,
            "linkid": task.linkid,
            "data": {
                "task_id": task.task_id,
                "linkid": task.linkid,
                "path": task.path,
                "status": task.status,
                "request_body": _safe_parse(task.request_body),
                "prompt": task.prompt,
                "sdk_output": task.sdk_output,
                "callback_body": _safe_parse(task.callback_body),
                "error_message": task.error_message,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
            },
        }

    return router
