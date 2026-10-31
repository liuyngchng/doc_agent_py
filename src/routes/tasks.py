"""
任务管理路由 — 查询进行中任务、取消、追踪、审计日志。
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request, Query, Depends
from fastapi.responses import JSONResponse, HTMLResponse

from ..config import Config
from ..logger import get_logger
from ..store import task_logs_store
from ..time import now_readable
from ..types import TaskLog
from .auth import build_api_token_dependency

log = get_logger()


def _now() -> str:
    return now_readable()


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
        request: Request,
        _auth: None = Depends(build_api_token_dependency(cfg.sys.api_token)),
    ):
        from_ip = request.client.host if request.client else ""
        tasks = task_logs_store.list_active()
        log.info("api_list_tasks from_ip=%s count=%d", from_ip, len(tasks))
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
                        "session_id": t.session_id,
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
        request: Request,
        _auth: None = Depends(build_api_token_dependency(cfg.sys.api_token)),
    ):
        from_ip = request.client.host if request.client else ""
        task = task_logs_store.get(task_id)

        if not task:
            log.warning("api_cancel_task_not_found from_ip=%s task_id=%s", from_ip, task_id)
            return JSONResponse(
                status_code=404,
                content={"code": 1001, "message": "任务不存在或已结束", "task_id": "", "linkid": ""},
            )
        if task.status != "pending":
            log.warning(
                "api_cancel_task_rejected from_ip=%s task_id=%s status=%s linkid=%s",
                from_ip, task_id, task.status, task.linkid,
            )
            return {
                "code": 1001,
                "message": "任务正在处理中，无法取消",
                "task_id": task_id,
                "linkid": task.linkid or "",
            }

        task_logs_store.update_status(task_id, "cancelled")
        log.info("api_cancel_task_ok from_ip=%s task_id=%s linkid=%s", from_ip, task_id, task.linkid)
        return {"code": 0, "message": "任务已取消", "task_id": task_id, "linkid": task.linkid or ""}

    @router.get("/api/v1/task/trace")
    async def task_trace(
        request: Request,
        taskid: str = Query(default=""),
        _auth: None = Depends(build_api_token_dependency(cfg.sys.api_token)),
    ):
        from_ip = request.client.host if request.client else ""
        if not taskid:
            log.warning("api_task_trace_missing_taskid from_ip=%s", from_ip)
            return JSONResponse(
                status_code=400,
                content={"code": 1001, "message": "缺少 taskid 参数", "task_id": "", "linkid": ""},
            )

        task = task_logs_store.get(taskid)
        if not task:
            log.warning("api_task_trace_not_found from_ip=%s task_id=%s", from_ip, taskid)
            return JSONResponse(
                status_code=404,
                content={"code": 1001, "message": "任务不存在", "task_id": "", "linkid": ""},
            )

        log.info(
            "api_task_trace from_ip=%s task_id=%s linkid=%s status=%s",
            from_ip, taskid, task.linkid, task.status,
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
                "session_id": task.session_id,
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

    # GET /api/v1/task/log/:task_id — 任务审计日志时间线（默认 HTML）
    @router.get("/api/v1/task/log/{task_id}")
    async def task_log(
        task_id: str,
        request: Request,
        format: str = Query(default="html"),
        _auth: None = Depends(build_api_token_dependency(cfg.sys.api_token)),
    ):
        from_ip = request.client.host if request.client else ""
        fmt = format.lower()

        task = task_logs_store.get(task_id)
        if not task:
            log.warning("api_task_log_not_found from_ip=%s task_id=%s", from_ip, task_id)
            if fmt == "html":
                return HTMLResponse(
                    content=_NOT_FOUND_HTML,
                    status_code=404,
                )
            return JSONResponse(
                status_code=404,
                content={"code": 1001, "message": "任务不存在", "task_id": "", "linkid": ""},
            )

        events = _build_timeline_events(task)
        linkid = task.linkid or ""

        log.info(
            "api_task_log from_ip=%s task_id=%s linkid=%s status=%s format=%s events=%d",
            from_ip, task_id, linkid, task.status, fmt, len(events),
        )

        if fmt == "html":
            return HTMLResponse(
                content=_render_timeline_html(task, events, linkid),
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
                "session_id": task.session_id,
                "path": task.path,
                "status": task.status,
                "callback_url": task.callback_url,
                "error_message": task.error_message,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "timeline": events,
            },
        }

    # GET /api/v1/task/link/log/:linkid — 按 linkid 查最新一条任务时间线
    @router.get("/api/v1/task/link/log/{linkid}")
    async def task_link_log(
        linkid: str,
        request: Request,
        format: str = Query(default="html"),
        _auth: None = Depends(build_api_token_dependency(cfg.sys.api_token)),
    ):
        from_ip = request.client.host if request.client else ""
        fmt = format.lower()

        task = task_logs_store.get_latest_by_linkid(linkid)
        if not task:
            log.warning("api_task_link_log_not_found from_ip=%s linkid=%s", from_ip, linkid)
            if fmt == "html":
                return HTMLResponse(
                    content=_NOT_FOUND_HTML,
                    status_code=404,
                )
            return JSONResponse(
                status_code=404,
                content={"code": 1001, "message": "任务不存在", "task_id": "", "linkid": ""},
            )

        events = _build_timeline_events(task)

        log.info(
            "api_task_link_log from_ip=%s task_id=%s linkid=%s status=%s format=%s events=%d",
            from_ip, task.task_id, linkid, task.status, fmt, len(events),
        )

        if fmt == "html":
            return HTMLResponse(
                content=_render_timeline_html(task, events, linkid),
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
                "session_id": task.session_id,
                "path": task.path,
                "status": task.status,
                "callback_url": task.callback_url,
                "error_message": task.error_message,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "timeline": events,
            },
        }

    return router


# ============================================================
# 时间线事件构建（精简为关键节点，去掉了智能体执行过程）
# ============================================================

def _build_timeline_events(task: TaskLog) -> list[dict]:
    events: list[dict] = []

    # 1. 任务创建
    events.append({
        "time": task.created_at,
        "type": "create",
        "title": "任务创建",
        "detail": "访问路径: " + (task.path or "-"),
    })

    # 2. 收到请求数据
    if task.request_body:
        events.append({
            "time": task.created_at,
            "type": "input",
            "title": "收到请求数据",
            "detail": _json_fmt(task.request_body),
        })

    # 3. 我方响应
    if task.request_response:
        events.append({
            "time": task.created_at,
            "type": "output",
            "title": "返回同步响应",
            "detail": _json_fmt(task.request_response),
        })

    # 4. 渲染提示词
    if task.prompt:
        events.append({
            "time": task.created_at,
            "type": "prompt",
            "title": "渲染提示词",
            "detail": task.prompt,
        })

    # 5. Agent 输出结果
    if task.sdk_output:
        events.append({
            "time": task.finished_at,
            "type": "output",
            "title": "Agent 输出结果",
            "detail": task.sdk_output,
        })

    # 6. 回调对方 URL
    if task.callback_url:
        detail = "POST " + task.callback_url
        if task.callback_body:
            detail += "\n\n回调体:\n" + _json_fmt(task.callback_body)
        if task.callback_response:
            detail += "\n\n回调响应:\n" + _json_fmt(task.callback_response)
        events.append({
            "time": task.finished_at,
            "type": "callback",
            "title": "回调对方 URL",
            "detail": detail,
        })

    # 7. 完成 / 失败
    final_title = _status_text(task.status)
    if task.status == "completed":
        final_type = "done"
    elif task.status in ("failed", "callback_fail"):
        final_type = "error"
    else:
        final_type = "info"
    events.append({
        "time": task.finished_at,
        "type": final_type,
        "title": final_title,
        "detail": task.error_message or "",
    })

    return events


# ============================================================
# HTML 时间线 CSS
# ============================================================

_TIMELINE_CSS = """*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f5f6fa;color:#333;padding:40px 20px}
.wrap{max-width:960px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.08);overflow:hidden}
.page-head{padding:28px 36px;background:linear-gradient(to right,#4b6cb7,#182848);color:#fff}
.page-head h1{font-size:1.4rem;display:flex;align-items:center;gap:10px}
.page-head .meta{margin-top:10px;font-size:.85rem;opacity:.85;display:flex;flex-direction:column;gap:6px}
.meta-line{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.meta-label{font-weight:600;opacity:.9}
.tl{padding:28px 36px}
.item{display:flex;gap:14px;padding:14px 0;border-left:2px solid #e0e0e0;margin-left:8px;position:relative}
.item:last-child{border-left-color:transparent}
.dot{width:16px;height:16px;border-radius:50%;background:#4b6cb7;flex-shrink:0;margin-left:-9px;margin-top:2px;position:relative;z-index:1}
.item.error .dot{background:#e53e3e}
.body{flex:1;min-width:0}
.ihead{display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:.92rem}
.time{font-family:monospace;font-size:.78rem;color:#888;white-space:nowrap}
.detail{background:#f7f8fa;border-radius:6px;padding:12px 16px;margin-top:8px;font-size:.82rem;line-height:1.6;white-space:pre-wrap;word-break:break-word;max-height:400px;overflow:auto;border:1px solid #edf2f7}
.foot{padding:16px 36px;border-top:1px solid #edf2f7;font-size:.78rem;color:#999;text-align:center}"""

_NOT_FOUND_HTML = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>任务不存在</title>
<style>{_TIMELINE_CSS}</style></head>
<body><div class="wrap"><div class="page-head"><h1>任务不存在</h1></div></div></body></html>"""


def _extract_title(request_body: str | None) -> str:
    if not request_body:
        return "-"
    try:
        obj = json.loads(request_body)
        title = (
            (obj.get("form_data") or {}).get("title") or
            (obj.get("form_data") or {}).get("标题") or
            obj.get("title") or
            obj.get("标题") or
            ""
        )
        return title or "-"
    except (json.JSONDecodeError, TypeError):
        return "-"


def _render_timeline_html(task: TaskLog, events: list[dict], linkid: str) -> str:
    items_html = ""
    for i, ev in enumerate(events):
        time_str = _fmt_ts(ev["time"]) if ev["time"] else ""
        etype = ev["type"] or ""
        detail_block = f'<pre class="detail">{_esc_html(ev["detail"])}</pre>' if ev["detail"] else ""
        items_html += f"""<div class="item {etype}">
        <div class="dot"></div>
        <div class="body">
          <div class="ihead"><span class="time">{_esc_html(time_str)}</span><strong>{i + 1}. {_esc_html(ev["title"])}</strong></div>
          {detail_block}
        </div>
      </div>
"""

    title = _extract_title(task.request_body)
    start_str = _fmt_ts(task.created_at)
    end_str = _fmt_ts(task.finished_at) if task.finished_at else "处理中"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>任务执行时间线 — {_esc_html(linkid)}</title>
<style>{_TIMELINE_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="page-head">
    <h1>&#129302; 任务执行时间线</h1>
    <div class="meta">
      <div class="meta-line"><span class="meta-label">标题:</span> {_esc_html(title)}</div>
      <div class="meta-line"><span class="meta-label">执行周期:</span> {_esc_html(start_str)} ~ {_esc_html(end_str)}</div>
      <div class="meta-line"><span class="meta-label">Task ID:</span> {_esc_html(task.task_id)} <span class="meta-label">Link ID:</span> {_esc_html(linkid)}</div>
    </div>
  </div>
  <div class="tl">{items_html}</div>
  <div class="foot">Powered by dp_agt_py</div>
</div>
</body>
</html>"""


# ============================================================
# helpers
# ============================================================

def _json_fmt(v: object) -> str:
    if isinstance(v, str):
        try:
            return json.dumps(json.loads(v), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            return v
    return json.dumps(v, ensure_ascii=False, indent=2, default=str)


def _status_text(s: str) -> str:
    if isinstance(s, str):
        return {
            "pending": "排队中",
            "processing": "处理中",
            "failed": "失败",
            "completed": "已完成",
            "callback_fail": "回调失败",
            "cancelled": "已取消",
        }.get(s, s or "-")
    return str(s) or "-"


def _esc_html(s: object) -> str:
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _fmt_ts(s: str | None) -> str:
    if not s:
        return "-"
    if re.match(r"^\d{2}:\d{2}:\d{2}", s) or re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", s):
        return s
    try:
        from datetime import datetime
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.strftime("%Y-%m-%d %H:%M:%S.") + f"{d.microsecond // 1000:03d}"
    except (ValueError, OSError):
        return s