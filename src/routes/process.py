"""
对外业务接口路由 — POST /api/v1/{path} 通配路由。

按访问路径匹配模板，校验参数后入队异步处理。
模拟操作和对外接口共用 submit_task。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse

from ..config import Config
from ..logger import get_logger
from ..store import interfaces_store, task_logs_store
from ..services.task_queue import enqueue, get_running_count, get_pending_count
from ..time import now_readable
from ..types import ProcessRequest, AgentInterface
from .auth import build_api_token_dependency

log = get_logger()


def _build_response(code: int, message: str, task_id: str, linkid: str) -> dict:
    resp: dict = {
        "code": code,
        "timestamp": now_readable(),
        "message": message,
    }
    if task_id:
        resp["task_id"] = task_id
    if linkid:
        resp["linkid"] = linkid
    return resp


def submit_task(
    interface_path: str,
    body: dict,
    cfg: Config,
) -> tuple[int, dict]:
    """通用任务提交逻辑：校验接口、队列上限，然后入队。模拟操作和对外接口都走这一套。
    返回 (http_status, response_body)。"""
    if not interface_path:
        return 404, _build_response(1001, "缺少访问路径", "", "")

    iface = interfaces_store.get_by_path(interface_path)
    if not iface:
        return 404, _build_response(1001, f"访问路径不存在: {interface_path}", "", "")

    if not body or not isinstance(body, dict):
        return 400, _build_response(1001, "请求体不能为空", "", "")

    linkid = str(body.get("linkid", ""))
    if not linkid:
        return 400, _build_response(1001, "缺少 linkid", "", "")
    if not body.get("callback_url"):
        return 400, _build_response(1001, "缺少 callback_url", "", "")

    # 检查队列上限
    max_pending = cfg.sys.max_pending_tasks
    active_count = get_running_count() + get_pending_count()
    if active_count >= max_pending:
        msg = (
            f"任务队列已满，当前处理中任务数 {active_count}/{max_pending}"
            f"（最大并发 {cfg.sys.max_concurrent_tasks}，最大排队 {max_pending}），请稍后重试"
        )
        return 503, _build_response(2002, msg, "", linkid)

    # 入队
    result = enqueue(iface, ProcessRequest(body))
    task_id = result["task_id"]

    resp = _build_response(0, "success", task_id, linkid)
    # 持久化同步响应
    task_logs_store.update_result(task_id, {"request_response": json.dumps(resp, ensure_ascii=False)})
    return 200, resp


def create_process_router(cfg: Config) -> APIRouter:
    router = APIRouter()

    @router.api_route("/api/v1/{rest_of_path:path}", methods=["POST"])
    async def process(
        rest_of_path: str,
        request: Request,
        _auth: None = Depends(build_api_token_dependency(cfg.sys.api_token)),
    ):
        interface_path = rest_of_path
        from_ip = request.client.host if request.client else ""

        try:
            body = await request.json()
        except Exception:
            body = {}

        log.debug("request_received from_ip=%s path=%s body=%s", from_ip, interface_path, body)

        status_code, resp = submit_task(interface_path, body, cfg)

        if status_code >= 400:
            log.warning("request_rejected from_ip=%s path=%s", from_ip, interface_path)
        else:
            log.info("api_request_completed from_ip=%s task_id=%s linkid=%s path=%s",
                     from_ip, resp.get("task_id"), resp.get("linkid"), interface_path)

        return JSONResponse(status_code=status_code, content=resp)

    return router