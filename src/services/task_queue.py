"""
异步任务队列

- 按 linkid 串行化：同一公文的多次提交排队顺序执行，保证 session 不冲突。
- 不同 linkid 之间仍可并发（受 max_concurrent_tasks 限制）。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import uuid6

from ..config import Config
from ..logger import get_logger
from ..store import task_logs_store, interfaces_store, sessions_store
from ..time import now_readable, now_ts
from ..types import (
    AgentInterface,
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

# ── per-linkid 串行化 ──
_link_chains: dict[str, asyncio.Future] = {}


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
    m = re.search(r"```json\s*([\s\S]*?)```", text)
    if m:
        return json.loads(m.group(1).strip())

    m = re.search(r"```\s*([\s\S]*?)```", text)
    if m:
        return json.loads(m.group(1).strip())

    return json.loads(text.strip())


async def _process_item(item: QueueItem) -> None:
    task_id, interface_path, iface, body = item.task_id, item.path, item.iface, item.body
    linkid = body.get("linkid", "")

    # 状态 → processing
    task_logs_store.update_status(task_id, TaskStatus.processing)
    log.info("task_processing task_id=%s linkid=%s path=%s", task_id, linkid, interface_path)

    # 渲染提示词
    rendered = render_prompt(iface.prompt, body)
    task_logs_store.update_result(task_id, {"prompt": rendered})

    # 查 sessions 表：同一 linkid 是否有已存在的 session
    existing_session = sessions_store.get_by_linkid(linkid)
    resume_session_id = existing_session.session_id if existing_session else None

    if resume_session_id:
        log.info("session_resume task_id=%s linkid=%s session_id=%s", task_id, linkid, resume_session_id)

    # 调用 Claude Agent SDK。
    # 通过 on_session_id 回调在首条流式消息拿到 session_id 的瞬间就写入 DB，
    # 这样任务仍在 processing 时，前端「对话」按钮就能实时看到会话。
    result: SdkResult = await run_agent(
        linkid, task_id, rendered, _cfg, resume_session_id,
        on_session_id=lambda sid: (
            sessions_store.upsert(sid, linkid),
            task_logs_store.update_result(task_id, {"session_id": sid}),
        ),
    )

    # 兜底：若 on_session_id 未触发（例如异常路径），任务结束后再写一次

    # 首条消息拿到 session_id 就立即写入 sessions 表
    if result.session_id:
        sessions_store.upsert(result.session_id, linkid)

    if not result.success:
        # LLM 调用本身失败
        err_msg = result.error_message or "Claude 处理失败"
        task_logs_store.update_result(task_id, {
            "status": TaskStatus.failed,
            "session_id": result.session_id,
            "sdk_output": result.raw_output,
            "error_message": err_msg,
            "finished_at": now_readable(),
        })

        fail_payload = _build_response(5000, err_msg, task_id, linkid, None)
        cb_result: CallbackResult = await deliver_callback(
            body.get("callback_url", ""),
            fail_payload,
            _cfg,
        )
        task_logs_store.update_result(task_id, {
            "status": TaskStatus.failed,
            "callback_body": cb_result.body,
            "callback_response": cb_result.response_body,
        })
        log.error("task_failed task_id=%s linkid=%s error=%s", task_id, linkid, err_msg)
        return

    # LLM 成功 — 暂不修改 status，等回调完成再统一设
    task_logs_store.update_result(task_id, {
        "session_id": result.session_id,
        "sdk_output": result.raw_output,
    })

    # 尝试解析 Claude 输出的 JSON
    try:
        parsed_data = _extract_json(result.raw_output)
    except Exception:
        parsed_data = {"raw_output": result.raw_output}

    # 回调
    success_payload = _build_response(0, "success", task_id, linkid, parsed_data)
    cb_result = await deliver_callback(
        body.get("callback_url", ""),
        success_payload,
        _cfg,
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

    log.info("task_done task_id=%s linkid=%s status=%s", task_id, linkid, final_status)


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

        # per-linkid 串行化：挂到该 linkid 的链后面
        linkid = item.body.get("linkid", "")
        prev = _link_chains.get(linkid)
        loop = asyncio.get_event_loop()

        async def _run(prev_fut: asyncio.Future | None, it: QueueItem) -> None:
            try:
                if prev_fut is not None:
                    await prev_fut
                await _process_item(it)
            finally:
                global _running
                _running -= 1
                if _link_chains.get(it.body.get("linkid", "")) is not None:
                    _link_chains.pop(it.body.get("linkid", ""), None)
                if _pending and _running < (_cfg.sys.max_concurrent_tasks if _cfg else 5):
                    await _pump()

        task = loop.create_task(_run(prev, item))
        _link_chains[linkid] = task


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
        session_id=None,
        path=iface.path,
        interface_id=iface.id,
        status=TaskStatus.pending,
        request_body=json.dumps(body, ensure_ascii=False),
        request_response=None,
        prompt="",
        sdk_output=None,
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