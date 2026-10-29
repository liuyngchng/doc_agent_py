"""
管理后台 API — 对应 TypeScript 版 `routes/admin.ts`。

接口 CRUD + 模拟操作 + 任务日志查询。
"""

import json
from pathlib import Path

import uuid6
from fastapi import APIRouter, Request, HTTPException

from ..config import Config
from ..logger import get_logger
from ..store import interfaces_store, task_logs_store
from ..services.prompt_engine import render_prompt
from ..services.sdk_runner import run_agent
from ..types import TaskLog, TaskStatus
from .auth import _require_api_auth, _require_admin

log = get_logger()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _safe_parse(s: str | None) -> object:
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


def create_admin_router(cfg: Config) -> APIRouter:
    router = APIRouter()

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

    # ---- 模拟操作 ----

    @router.get("/admin/mock")
    async def get_mock(request: Request):
        """读取 data/mock.json 作为模拟请求体默认值（文件可不存在）。"""
        await _require_api_auth(request)
        mock_file = Path(cfg.sys.data_dir) / "mock.json"
        if not mock_file.exists():
            raise HTTPException(status_code=404, detail={"code": 1001, "message": "mock.json 不存在"})
        try:
            raw = mock_file.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            return {"code": 0, "data": parsed}
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": 1001, "message": f"mock.json 解析失败: {exc}"},
            )

    @router.post("/admin/simulate")
    async def simulate(request: Request):
        await _require_api_auth(request)
        body = await request.json()
        interface_path = body.get("interface_path")
        req_linkid = body.get("linkid")
        request_body = body.get("request_body")

        if not interface_path:
            raise HTTPException(status_code=400, detail={"code": 1001, "message": "缺少 interface_path"})

        iface = interfaces_store.get_by_path(interface_path)
        if not iface:
            raise HTTPException(status_code=404, detail={"code": 1001, "message": f"接口不存在: {interface_path}"})

        if not request_body or not isinstance(request_body, dict):
            raise HTTPException(status_code=400, detail={"code": 1001, "message": "请求体不能为空"})

        task_id = uuid6.uuid7().hex
        linkid = req_linkid or "sim-" + task_id[:8]
        now = _now()

        # 持久化 task_log
        log_entry = TaskLog(
            task_id=task_id,
            linkid=linkid,
            path=iface.path,
            interface_id=iface.id,
            status=TaskStatus.pending,
            request_body=json.dumps(request_body, ensure_ascii=False),
            prompt="",
            sdk_output=None,
            sdk_trace=None,
            callback_body=None,
            callback_url="",  # 模拟操作不回调
            error_message=None,
            created_at=now,
            finished_at=None,
        )
        task_logs_store.create(log_entry)

        log.info("simulate_task_created task_id=%s linkid=%s path=%s", task_id, linkid, iface.path)

        # 立即返回
        response = {"code": 0, "message": "任务已创建", "task_id": task_id, "linkid": linkid}

        # 后台异步执行
        import asyncio

        async def _run_simulation():
            try:
                task_logs_store.update_status(task_id, TaskStatus.processing)
                log.info("simulate_task_processing task_id=%s", task_id)

                rendered = render_prompt(iface.prompt, request_body)
                task_logs_store.update_result(task_id, {"prompt": rendered})

                result: SdkResult = await run_agent(task_id, rendered, cfg)

                if not result.success:
                    task_logs_store.update_result(task_id, {
                        "status": TaskStatus.failed,
                        "sdk_output": result.raw_output,
                        "sdk_trace": result.trace,
                        "error_message": result.error_message or "Claude 处理失败",
                        "finished_at": _now(),
                    })
                    log.error("simulate_task_failed task_id=%s error=%s", task_id, result.error_message)
                else:
                    task_logs_store.update_result(task_id, {
                        "status": TaskStatus.success,
                        "sdk_output": result.raw_output,
                        "sdk_trace": result.trace,
                        "finished_at": _now(),
                    })
                    log.info("simulate_task_success task_id=%s output_len=%d", task_id, len(result.raw_output))
            except Exception as exc:
                task_logs_store.update_result(task_id, {
                    "status": TaskStatus.failed,
                    "error_message": str(exc),
                    "finished_at": _now(),
                })
                log.error("simulate_task_exception task_id=%s error=%s", task_id, exc)

        asyncio.create_task(_run_simulation())
        return response

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

    return router
