"""
管理后台 API — 对应 TypeScript 版 `routes/admin.ts`。

接口 CRUD + 任务日志查询。
"""

import json
from fastapi import APIRouter, Request, HTTPException

from ..logger import get_logger
from ..store import interfaces_store, task_logs_store
from .auth import _require_api_auth, _require_admin

log = get_logger()

router = APIRouter()


def _safe_parse(s: str | None) -> object:
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


# ---- 接口管理 ----

@router.get("/admin/interfaces")
async def list_interfaces(request: Request):
    await _require_api_auth(request)
    interfaces = interfaces_store.list()
    return {"code": 0, "data": [i.__dict__ for i in interfaces]}


@router.get("/admin/interfaces/{iface_id}")
async def get_interface(iface_id: str, request: Request):
    await _require_api_auth(request)
    iface = interfaces_store.get(iface_id)
    if not iface:
        raise HTTPException(status_code=404, detail={"code": 1001, "message": "接口不存在"})
    return {"code": 0, "data": iface.__dict__}


@router.post("/admin/interfaces")
async def create_interface(request: Request):
    await _require_admin(request)
    body = await request.json()
    name = body.get("name")
    path = body.get("path")
    prompt = body.get("prompt")
    if not name or not path or not prompt:
        raise HTTPException(status_code=400, detail={"code": 1001, "message": "缺少 name / path / prompt"})

    existing = interfaces_store.get_by_path(path)
    if existing:
        raise HTTPException(status_code=400, detail={"code": 1001, "message": f"接口 path 已存在: {path}"})

    created = interfaces_store.create(name, path, prompt)
    log.info("interface_created name=%s path=%s", name, path)
    return {"code": 0, "data": created.__dict__}


@router.put("/admin/interfaces/{iface_id}")
async def update_interface(iface_id: str, request: Request):
    await _require_admin(request)
    body = await request.json()
    name = body.get("name")
    path = body.get("path")
    prompt = body.get("prompt")
    if not name or not path or not prompt:
        raise HTTPException(status_code=400, detail={"code": 1001, "message": "缺少 name / path / prompt"})

    updated = interfaces_store.update(iface_id, name, path, prompt)
    if not updated:
        raise HTTPException(status_code=404, detail={"code": 1001, "message": "接口不存在"})

    log.info("interface_updated name=%s path=%s", name, path)
    return {"code": 0, "data": updated.__dict__}


@router.delete("/admin/interfaces/{iface_id}")
async def delete_interface(iface_id: str, request: Request):
    await _require_admin(request)
    ok = interfaces_store.remove(iface_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 1001, "message": "接口不存在"})
    log.info("interface_deleted id=%s", iface_id)
    return {"code": 0, "message": "已删除"}


# ---- 任务日志 ----

@router.get("/admin/tasks")
async def list_tasks(request: Request, limit: int = 100):
    await _require_api_auth(request)
    limit = min(max(limit, 1), 500)
    tasks = task_logs_store.list(limit)
    return {
        "code": 0,
        "data": [
            {
                "task_id": t.task_id,
                "linkid": t.linkid,
                "path": t.path,
                "status": t.status,
                "callback_url": t.callback_url,
                "error_message": t.error_message,
                "created_at": t.created_at,
                "finished_at": t.finished_at,
            }
            for t in tasks
        ],
    }


@router.get("/admin/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    await _require_api_auth(request)
    task = task_logs_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 1001, "message": "任务不存在"})

    data = task.__dict__
    data["request_body"] = _safe_parse(task.request_body)
    data["callback_body"] = _safe_parse(task.callback_body)
    data["sdk_trace"] = _safe_parse(task.sdk_trace)
    return {"code": 0, "data": data}
