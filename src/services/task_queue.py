"""
异步任务队列 — 对应 TypeScript 版 `task-queue.ts`。

基于 asyncio 的轻量内存队列，配合 SQLite 持久化状态。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import uuid6

from ..config import Config
from ..logger import get_logger
from ..store import task_logs_store
from ..types import (
    AgentInterface,
    ApiResponse,
    ProcessRequest,
    QueueItem,
    TaskLog,
    TaskStatus,
)
from .callback import deliver_callback, CallbackResult
from .prompt_engine import render_prompt
from .sdk_runner import run_agent, SdkResult

log = get_logger()

_cfg: Config | None = None
_running: int = 0
_pending: list[QueueItem] = []


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _build_response(
    code: int,
    message: str,
    task_id: str,
    linkid: str,
    data: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "timestamp": _now().replace("T", " ")[:19],
        "message": message,
        "task_id": task_id,
        "linkid": linkid,
        "data": data if data is not None else {},
    }


def _extract_json(text: str) -> Any:
    """从 Claude 输出中提取 JSON 对象。"""
    # 尝试 ```json 代码块
    m = re.search(r"```json\s*([\s\S]*?)```", text)
    if m:
        return json.loads(m.group(1).strip())

    # 尝试 ``` 代码块（无语言标注）
    m = re.search(r"```\s*([\s\S]*?)```", text)
    if m:
        return json.loads(m.group(1).strip())

    # 尝试直接解析整个文本
    return json.loads(text.strip())


async def _process_item(item: QueueItem) -> None:
    global _running
    task_id, interface_path, iface, body = item.task_id, item.path, item.iface, item.body

    # 状态 → processing
    task_logs_store.update_status(task_id, TaskStatus.processing)
    log.info("task_processing task_id=%s linkid=%s path=%s", task_id, body.get("linkid"), interface_path)

    # 渲染提示词
    rendered = render_prompt(iface.prompt, body)
    task_logs_store.update_result(task_id, {"prompt": rendered})

    # 调用 Claude Code Agent SDK
    assert _cfg is not None
    result: SdkResult = await run_agent(task_id, rendered, _cfg)

    if not result.success:
        # LLM 调用本身失败
        err_msg = result.error_message or "Claude 处理失败"
        task_logs_store.update_result(task_id, {
            "status": TaskStatus.failed,
            "sdk_output": result.raw_output,
            "sdk_trace": result.trace,
            "error_message": err_msg,
            "finished_at": _now(),
        })

        fail_payload = _build_response(5000, err_msg, task_id, body.get("linkid", ""), None)
        cb_result: CallbackResult = await deliver_callback(
            body.get("callback_url", ""),
            fail_payload,  # type: ignore[arg-type]
            _cfg,
        )
        task_logs_store.update_result(task_id, {
            "status": TaskStatus.failed,
            "callback_body": cb_result.body,
        })
        log.error("task_failed task_id=%s error=%s", task_id, err_msg)
        return

    # LLM 成功，记录输出
    task_logs_store.update_result(task_id, {
        "status": TaskStatus.success,
        "sdk_output": result.raw_output,
        "sdk_trace": result.trace,
    })

    # 尝试解析 Claude 输出的 JSON
    try:
        parsed_data = _extract_json(result.raw_output)
    except Exception:
        parsed_data = {"raw_output": result.raw_output}

    # 回调
    success_payload = _build_response(0, "success", task_id, body.get("linkid", ""), parsed_data)
    cb_result = await deliver_callback(
        body.get("callback_url", ""),
        success_payload,  # type: ignore[arg-type]
        _cfg,
    )

    final_status = TaskStatus.completed if cb_result.success else TaskStatus.callback_fail
    update_fields: dict[str, Any] = {
        "status": final_status,
        "callback_body": cb_result.body,
        "finished_at": _now(),
    }
    if cb_result.error_msg:
        update_fields["error_message"] = cb_result.error_msg
    task_logs_store.update_result(task_id, update_fields)

    log.info("task_done task_id=%s status=%s", task_id, final_status)


async def _pump() -> None:
    """消费 pending 队列中的任务。"""
    global _running, _pending
    assert _cfg is not None
    max_concurrent = _cfg.sys.max_concurrent_tasks
    max_pending = _cfg.sys.max_pending_tasks

    while _running < max_concurrent and _pending:
        item = _pending.pop(0)
        if _running >= max_pending:
            _pending.insert(0, item)
            break

        _running += 1
        asyncio.create_task(_process_item_and_decr(item))


async def _process_item_and_decr(item: QueueItem) -> None:
    global _running, _pending
    try:
        await _process_item(item)
    finally:
        _running -= 1
        if _pending and _running < (_cfg.sys.max_concurrent_tasks if _cfg else 5):
            await _pump()


def init_task_queue(cfg: Config) -> None:
    """初始化任务队列（设置配置引用）。"""
    global _cfg
    _cfg = cfg


def enqueue(iface: AgentInterface, body: ProcessRequest) -> dict[str, str]:
    """将任务入队并返回 task_id。"""
    task_id = uuid6.uuid7().hex
    now = _now()

    # 持久化
    log_entry = TaskLog(
        task_id=task_id,
        linkid=body.get("linkid", ""),
        path=iface.path,
        interface_id=iface.id,
        status=TaskStatus.pending,
        request_body=json.dumps(body, ensure_ascii=False),
        prompt="",
        sdk_output=None,
        sdk_trace=None,
        callback_body=None,
        callback_url=body.get("callback_url", ""),
        error_message=None,
        created_at=now,
        finished_at=None,
    )
    task_logs_store.create(log_entry)

    # 入队
    _pending.append(QueueItem(task_id=task_id, path=iface.path, iface=iface, body=body))

    # 触发消费
    asyncio.create_task(_pump())

    return {"task_id": task_id}


def get_running_count() -> int:
    return _running


def get_pending_count() -> int:
    return len(_pending)
