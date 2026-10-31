"""
管理后台 API — 模板管理、模拟操作、任务监控、任务审查、系统设置、用户管理。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import Config
from ..logger import get_logger
from ..store import (
    interfaces_store,
    task_logs_store,
    users_store,
    sessions_store,
    config_store,
    hash_password,
    sanitize_linkid,
)
from ..time import now_readable
from ..services.sse_bus import subscribe, unsubscribe, sse_encode
from .auth import get_current_user, _require_api_auth, _require_admin, _validate_password_strength
from .process import submit_task

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


def _detect_primary_ip() -> str:
    """探测主要 IP（优先内网地址）。"""
    candidates: list[str] = []
    try:
        for name in socket.if_nameindex():
            try:
                addrs = socket.getaddrinfo(socket.if_indextoname(name.index), None, socket.AF_INET)
                for addr in addrs:
                    ip = addr[4][0]
                    if ip.startswith("127."):
                        continue
                    if ip.startswith("172.") or ip.startswith("10.") or ip.startswith("192.168."):
                        candidates.append(ip)
                    else:
                        candidates.insert(0, ip)
            except OSError:
                pass
    except Exception:
        pass
    return candidates[0] if candidates else "127.0.0.1"


def _iface_to_dict(iface: Any) -> dict:
    return {
        "id": iface.id,
        "name": iface.name,
        "path": iface.path,
        "prompt": iface.prompt,
        "created_at": iface.created_at,
        "updated_at": iface.updated_at,
        "created_ts": iface.created_ts,
        "updated_ts": iface.updated_ts,
    }


def create_admin_router(cfg: Config) -> APIRouter:
    router = APIRouter()

    # ============================================================
    # 导航信息（所有登录用户可访问）
    # ============================================================

    @router.get("/admin/nav")
    async def nav(request: Request):
        cu = get_current_user(request)
        is_admin = cu and cu.get("role") == 1
        tabs: list[dict] = []
        if is_admin:
            tabs += [
                {"id": "interfaces", "icon": "fa-brain", "label": "模板管理"},
                {"id": "simulate", "icon": "fa-flask", "label": "操作模拟"},
                {"id": "tasks", "icon": "fa-list-alt", "label": "任务监控"},
            ]
        tabs.append({"id": "audit", "icon": "fa-search", "label": "任务审查"})
        if is_admin:
            tabs += [
                {"id": "sys", "icon": "fa-sliders-h", "label": "系统设置"},
                {"id": "users", "icon": "fa-users", "label": "用户管理"},
            ]
        default_tab = "interfaces" if is_admin else "audit"
        return {"code": 0, "data": {"role": 1 if is_admin else 0, "default_tab": default_tab, "tabs": tabs}}

    @router.get("/admin/server-info")
    async def server_info():
        cert_file = cfg.server.cert_file
        key_file = cfg.server.key_file
        protocol = "https" if (cert_file and key_file and Path(cert_file).exists() and Path(key_file).exists()) else "http"
        host = cfg.server.self_ip or _detect_primary_ip()
        return {"code": 0, "data": {"protocol": protocol, "host": host, "port": cfg.server.port}}

    # ============================================================
    # 接口管理（需登录）
    # ============================================================

    @router.get("/admin/interfaces")
    async def list_interfaces(request: Request):
        await _require_api_auth(request)
        interfaces = interfaces_store.list()
        return {"code": 0, "data": [_iface_to_dict(i) for i in interfaces]}

    @router.get("/admin/interfaces/{iface_id}")
    async def get_interface(iface_id: str, request: Request):
        await _require_api_auth(request)
        iface = interfaces_store.get(iface_id)
        if not iface:
            raise HTTPException(status_code=404, detail={"code": 1001, "message": "访问路径不存在"})
        return {"code": 0, "data": _iface_to_dict(iface)}

    @router.post("/admin/interfaces")
    async def create_interface(request: Request):
        await _require_admin(request)
        body = await request.json()
        name = body.get("name", "")
        path = body.get("path", "")
        prompt = body.get("prompt", "")
        if not name or not path or not prompt:
            raise HTTPException(status_code=400, detail={"code": 1001, "message": "缺少 name / path / prompt"})

        existing = interfaces_store.get_by_path(path)
        if existing:
            raise HTTPException(status_code=400, detail={"code": 1001, "message": f"访问路径 path 已存在: {path}"})

        created = interfaces_store.create(name, path, prompt)
        log.info("interface_created name=%s path=%s", name, path)
        return {"code": 0, "data": _iface_to_dict(created)}

    @router.put("/admin/interfaces/{iface_id}")
    async def update_interface(iface_id: str, request: Request):
        await _require_admin(request)
        body = await request.json()
        name = body.get("name", "")
        path = body.get("path", "")
        prompt = body.get("prompt", "")
        if not name or not path or not prompt:
            raise HTTPException(status_code=400, detail={"code": 1001, "message": "缺少 name / path / prompt"})

        updated = interfaces_store.update(iface_id, name, path, prompt)
        if not updated:
            raise HTTPException(status_code=404, detail={"code": 1001, "message": "访问路径不存在"})

        log.info("interface_updated name=%s path=%s", name, path)
        return {"code": 0, "data": _iface_to_dict(updated)}

    @router.delete("/admin/interfaces/{iface_id}")
    async def delete_interface(iface_id: str, request: Request):
        await _require_admin(request)
        ok = interfaces_store.remove(iface_id)
        if not ok:
            raise HTTPException(status_code=404, detail={"code": 1001, "message": "访问路径不存在"})
        log.info("interface_deleted id=%s", iface_id)
        return {"code": 0, "message": "已删除"}

    # ============================================================
    # 模拟操作
    # ============================================================

    @router.get("/admin/mock")
    async def get_mock(request: Request):
        """读取 data/mock.json 作为模拟请求体默认值。"""
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
        interface_path = body.get("interface_path", "")
        request_body = body.get("request_body") or {}

        log.debug("simulate_request_received path=%s body=%s", interface_path, request_body)

        status_code, resp = submit_task(interface_path, request_body, cfg)

        if status_code >= 400:
            log.warning("simulate_request_rejected path=%s", interface_path)
        else:
            log.info(
                "simulate_task_enqueued task_id=%s linkid=%s path=%s",
                resp.get("task_id"), resp.get("linkid"), interface_path,
            )

        return JSONResponse(status_code=status_code, content=resp)

    # ============================================================
    # 任务监控
    # ============================================================

    @router.get("/admin/tasks")
    async def list_tasks(
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10, ge=1, le=100),
    ):
        await _require_api_auth(request)
        items, total = task_logs_store.list_paged((page - 1) * page_size, page_size)
        return {
            "code": 0,
            "data": {
                "total": total,
                "page": page,
                "page_size": page_size,
                "items": [
                    {
                        "task_id": t.task_id,
                        "linkid": t.linkid,
                        "session_id": t.session_id,
                        "title": _extract_title(t.request_body),
                        "path": t.path,
                        "status": t.status,
                        "callback_url": t.callback_url,
                        "error_message": t.error_message,
                        "created_at": t.created_at,
                        "finished_at": t.finished_at,
                    }
                    for t in items
                ],
            },
        }

    @router.get("/admin/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        await _require_api_auth(request)
        task = task_logs_store.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail={"code": 1001, "message": "任务不存在"})
        return {
            "code": 0,
            "data": {
                **task.__dict__,
                "title": _extract_title(task.request_body),
                "request_body": _safe_parse(task.request_body),
                "request_response": _safe_parse(task.request_response),
                "callback_body": _safe_parse(task.callback_body),
                "callback_response": _safe_parse(task.callback_response),
            },
        }

    # ---- 会话记录（SDK 原生对话） ----

    @router.get("/admin/sessions/{session_id}/messages")
    async def session_messages(session_id: str, request: Request):
        await _require_api_auth(request)
        if not session_id:
            raise HTTPException(status_code=400, detail={"code": 1001, "message": "缺少 session_id"})

        session = sessions_store.get(session_id)
        work_dir = str(Path(cfg.sys.data_dir) / "tasks" / sanitize_linkid(session.linkid if session else ""))

        try:
            from claude_agent_sdk import get_session_messages
            messages = await get_session_messages(session_id, dir=work_dir, include_system_messages=False)
            return {"code": 0, "data": {"session_id": session_id, "messages": messages}}
        except Exception as err:
            msg = str(err)
            log.error("session_messages_error session_id=%s error=%s", session_id, msg)
            raise HTTPException(status_code=500, detail={"code": 1001, "message": f"读取会话记录失败: {msg}"})

    # SSE 实时推送（任务处理中时前端订阅）
    @router.get("/admin/sessions/{session_id}/stream")
    async def session_stream(session_id: str, request: Request):
        await _require_api_auth(request)
        if not session_id:
            raise HTTPException(status_code=400, detail={"code": 1001, "message": "缺少 session_id"})

        import asyncio

        session = sessions_store.get(session_id)
        work_dir = str(Path(cfg.sys.data_dir) / "tasks" / sanitize_linkid(session.linkid if session else ""))

        async def _event_stream():
            q: asyncio.Queue = subscribe(session_id)
            try:
                # 先推全量快照，前端据此渲染已有历史
                try:
                    from claude_agent_sdk import get_session_messages
                    messages = await get_session_messages(session_id, dir=work_dir, include_system_messages=False)
                    yield sse_encode({"type": "snapshot", "messages": messages})
                except Exception as err:
                    yield sse_encode({"type": "error", "message": str(err)})

                # 不断推送增量消息
                while True:
                    msg = await q.get()
                    yield sse_encode(msg)
                    m = msg if isinstance(msg, dict) else {}
                    if m.get("type") == "end":
                        break
            finally:
                unsubscribe(session_id, q)

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ============================================================
    # 任务审查（所有登录用户可访问）
    # ============================================================

    @router.get("/admin/audit")
    async def audit_list(
        request: Request,
        task_id: str = Query(default=""),
        linkid: str = Query(default=""),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10, ge=1, le=100),
    ):
        await _require_api_auth(request)
        tid = task_id.strip()
        lid = linkid.strip()
        items, total = task_logs_store.search_paged(tid, lid, (page - 1) * page_size, page_size)
        return {
            "code": 0,
            "data": {
                "total": total,
                "page": page,
                "page_size": page_size,
                "items": [
                    {
                        "task_id": t.task_id,
                        "linkid": t.linkid,
                        "session_id": t.session_id,
                        "title": _extract_title(t.request_body),
                        "path": t.path,
                        "status": t.status,
                        "created_at": t.created_at,
                        "finished_at": t.finished_at,
                    }
                    for t in items
                ],
            },
        }

    @router.get("/admin/audit/{task_id}")
    async def audit_detail(task_id: str, request: Request):
        await _require_api_auth(request)
        task = task_logs_store.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail={"code": 1001, "message": "任务不存在"})
        return {
            "code": 0,
            "data": {
                "task_id": task.task_id,
                "linkid": task.linkid,
                "session_id": task.session_id,
                "title": _extract_title(task.request_body),
                "path": task.path,
                "status": task.status,
                "callback_url": task.callback_url,
                "error_message": task.error_message,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "request_body": _safe_parse(task.request_body),
                "request_response": _safe_parse(task.request_response),
                "prompt": task.prompt,
                "sdk_output": task.sdk_output,
                "callback_body": _safe_parse(task.callback_body),
                "callback_response": _safe_parse(task.callback_response),
            },
        }

    # ============================================================
    # 系统设置（仅管理员）
    # ============================================================

    @router.get("/admin/sys-config")
    async def get_sys_config(request: Request):
        await _require_admin(request)
        name = config_store.get("sys.name") or ""
        api_auth_enabled = config_store.get("api_auth_enabled")
        api_auth_token = config_store.get("api_auth_token")
        return {
            "code": 0,
            "data": {
                "sys": {"name": name},
                "api": {
                    "auth_enabled": api_auth_enabled == "1",
                    "auth_token": api_auth_token or "",
                },
            },
        }

    @router.put("/admin/sys-config")
    async def put_sys_config(request: Request):
        await _require_admin(request)
        body = await request.json()
        sys_cfg = body.get("sys") or {}
        api_cfg = body.get("api") or {}

        if isinstance(sys_cfg, dict) and sys_cfg.get("name", "").strip():
            config_store.set("sys.name", sys_cfg["name"].strip(), "系统名称")

        if isinstance(api_cfg, dict):
            if "auth_enabled" in api_cfg:
                config_store.set(
                    "api_auth_enabled",
                    "1" if api_cfg["auth_enabled"] else "0",
                    "对外 API 是否启用 X-Api-Key 认证",
                )
            if "auth_token" in api_cfg and isinstance(api_cfg["auth_token"], str):
                config_store.set("api_auth_token", api_cfg["auth_token"], "对外 API 认证 Token")

        cu = get_current_user(request)
        log.info(
            "admin_update_sys_config admin=%s sys_name=%s api_auth_enabled=%s",
            cu.get("uname") if cu else "?",
            sys_cfg.get("name"),
            api_cfg.get("auth_enabled"),
        )
        return {"code": 0, "message": "保存成功"}

    # ============================================================
    # 用户管理（仅管理员）
    # ============================================================

    @router.get("/admin/users")
    async def list_users(request: Request):
        await _require_admin(request)
        users = users_store.list()
        return {
            "code": 0,
            "data": [
                {
                    "id": u.id,
                    "user_name": u.username,
                    "role": u.role,
                    "note": u.note,
                    "created_at": u.created_at,
                }
                for u in users
            ],
        }

    @router.post("/admin/users")
    async def create_user(request: Request):
        await _require_admin(request)
        body = await request.json()
        user_name = body.get("user_name", "")
        user_pwd = body.get("user_pwd", "")
        role = body.get("role", 0)
        note = body.get("note", "")

        if not user_name or not user_pwd:
            raise HTTPException(status_code=400, detail={"code": 1001, "message": "用户名和密码不能为空"})

        from .auth import _validate_password_strength
        strength_err = _validate_password_strength(user_pwd)
        if strength_err:
            raise HTTPException(status_code=400, detail={"code": 1001, "message": strength_err})

        if users_store.get(user_name):
            raise HTTPException(status_code=409, detail={"code": 1001, "message": "用户名已存在"})

        hash = hash_password(user_pwd)
        ok = users_store.create(user_name, hash, 1 if role == 1 else 0, note or "")
        if not ok:
            raise HTTPException(status_code=500, detail={"code": 1001, "message": "创建失败"})

        cu = get_current_user(request)
        log.info("admin_create_user admin=%s target=%s role=%d", cu.get("uname") if cu else "?", user_name, role)
        return {"code": 0, "message": "添加成功"}

    @router.delete("/admin/users/{username}")
    async def delete_user(username: str, request: Request):
        await _require_admin(request)
        if username == "admin":
            raise HTTPException(status_code=400, detail={"code": 1001, "message": "不能删除 admin 用户"})

        ok = users_store.remove(username)
        if not ok:
            raise HTTPException(status_code=404, detail={"code": 1001, "message": "用户不存在"})

        cu = get_current_user(request)
        log.info("admin_delete_user admin=%s target=%s", cu.get("uname") if cu else "?", username)
        return {"code": 0, "message": "已删除"}

    return router