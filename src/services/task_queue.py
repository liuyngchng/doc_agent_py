"""
异步任务队列 — 基于 asyncio 的轻量内存队列，配合 SQLite 持久化状态。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import uuid6

from ..config import Config
from ..logger import get_logger
from ..store import task_logs_store, interfaces_store
from ..time import now_readable, now_ts
from ..types import (
    AgentInterface,
    ApiResponse,
    ProcessRequest,
    QueueItem,
    TaskLog,
    TaskStatus,
    StreamEntry,
)
from .callback import deliver_callback, CallbackResult
from .prompt_engine import render_prompt
from .sdk_runner import run_agent, SdkResult

log = get_logger()

_cfg: Config | None = None
_running: int = 0
_pending: list[QueueItem] = []


def _build_response(
    code: int,
    message: str,
    task_id: str,
    linkid: str,
    data: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "timestamp": now_readable(),
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

    # 调用 Claude Code Agent SDK（带流式回调）
    assert _cfg is not None

    def _on_stream(entries: list[StreamEntry]) -> None:
        task_logs_store.append_stream(task_id, entries)

    result: SdkResult = await run_agent(task_id, rendered, _cfg, on_stream_entry=_on_stream)

    if not result.success:
        # LLM 调用本身失败
        err_msg = result.error_message or "Claude 处理失败"
        task_logs_store.update_result(task_id, {
            "status": TaskStatus.failed,
            "sdk_output": result.raw_output,
            "sdk_trace": result.trace,
            "error_message": err_msg,
            "finished_at": now_readable(),
        })

        fail_payload = _build_response(5000, err_msg, task_id, body.get("linkid", ""), None)
        cb_result: CallbackResult = await deliver_callback(
            body.get("callback_url", ""),
            fail_payload,
            _cfg,
            on_stream=_on_stream,
        )
        task_logs_store.update_result(task_id, {
            "status": TaskStatus.failed,
            "callback_body": cb_result.body,
            "callback_response": cb_result.response_body,
        })
        log.error("task_failed task_id=%s error=%s", task_id, err_msg)
        return

    # LLM 成功 — 暂不修改 status，等回调完成再统一设
    task_logs_store.update_result(task_id, {
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
        success_payload,
        _cfg,
        on_stream=_on_stream,
    )

    final_status = TaskStatus.completed if cb_result.success else TaskStatus.callback_fail
    update_fields: dict[str, Any] = {
        "status": final_status,
        "callback_body": cb_result.body,
        "callback_response": cb_result.response_body,
        "finished_at": now_readable(),
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
    """初始化任务队列（设置配置引用），并恢复上次未完成的任务。"""
    global _cfg
    _cfg = cfg
    _recover_orphaned_tasks()


def start_recovered_pump() -> None:
    """事件循环启动后调用：消费恢复的任务（若有）。"""
    if _pending and _running < (_cfg.sys.max_concurrent_tasks if _cfg else 5):
        asyncio.create_task(_pump())


def _recover_orphaned_tasks() -> None:
    """启动时恢复上次未完成的任务。"""
    active = task_logs_store.list_active()
    if not active:
        return

    resumed = 0
    killed = 0

    for task in active:
        if task.status == TaskStatus.pending:
            iface = interfaces_store.get(task.interface_id or "")
            if not iface:
                task_logs_store.update_result(task.task_id, {
                    "status": TaskStatus.failed,
                    "error_message": "服务重启：关联的接口模板已被删除",
                    "finished_at": now_readable(),
                    "finished_ts": now_ts(),
                })
                killed += 1
                continue

            try:
                body = ProcessRequest(json.loads(task.request_body))
            except (json.JSONDecodeError, TypeError):
                continue

            _pending.append(QueueItem(task_id=task.task_id, path=task.path, iface=iface, body=body))
            resumed += 1
        elif task.status == TaskStatus.processing:
            task_logs_store.update_result(task.task_id, {
                "status": TaskStatus.failed,
                "error_message": "服务重启：任务被中断（原进程已退出）",
                "finished_at": now_readable(),
                "finished_ts": now_ts(),
            })
            killed += 1

    log.info("task_queue_recovered resumed=%d killed=%d", resumed, killed)


def enqueue(iface: AgentInterface, body: ProcessRequest) -> dict[str, str]:
    """将任务入队并返回 task_id。"""
    task_id = uuid6.uuid7().hex
    now = now_readable()
    now_t = now_ts()

    # 持久化
    log_entry = TaskLog(
        task_id=task_id,
        linkid=body.get("linkid", ""),
        path=iface.path,
        interface_id=iface.id,
        status=TaskStatus.pending,
        request_body=json.dumps(body, ensure_ascii=False),
        request_response=None,
        prompt="",
        sdk_output=None,
        sdk_trace=None,
        sdk_stream=None,
        callback_body=None,
        callback_response=None,
        callback_url=body.get("callback_url", ""),
        error_message=None,
        created_at=now,
        finished_at=None,
        created_ts=now_t,
        finished_ts=None,
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