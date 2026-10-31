"""
认证路由 — Cookie/Bearer/API-Key 多种认证方式，admin 首次登录强制改密。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Any

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, Response

from ..logger import get_logger
from ..store import users_store, config_store, hash_password, compare_password

log = get_logger()

router = APIRouter()

# Token 有效期：持久化密钥 7 天，临时密钥 2h
LONG_TTL = 7 * 24 * 3600
SHORT_TTL = 2 * 3600
MUST_CHANGE_PWD_WINDOW = 2 * 3600

# 模块级缓存
_cached_secret: dict | None = None


def _get_or_create_secret() -> dict:
    global _cached_secret
    if _cached_secret:
        return _cached_secret

    persisted = config_store.get("token_secret")
    if persisted:
        _cached_secret = {"secret": bytes.fromhex(persisted), "temporary": False}
        return _cached_secret

    random_bytes = os.urandom(32)
    log.warning("auth_auto_generated_key — 使用临时随机密钥（token 2h 有效），admin 修改密码后自动持久化")
    _cached_secret = {"secret": random_bytes, "temporary": True}
    return _cached_secret


def persist_secret() -> None:
    info = _get_or_create_secret()
    if info["temporary"]:
        config_store.set("token_secret", info["secret"].hex(), "admin 改密后持久化的 token 签名密钥")
        global _cached_secret
        _cached_secret = {"secret": info["secret"], "temporary": False}
        log.info("auth_key_persisted — admin 修改密码后，临时密钥已持久化到数据库")


def _sign_token(user_id: int, username: str, role: int, user_version: int, secret: bytes, temporary: bool) -> str:
    ttl = SHORT_TTL if temporary else LONG_TTL
    payload = {
        "uid": user_id,
        "uname": username,
        "role": role,
        "uv": user_version,
        "exp": int(time.time()) + ttl,
        "salt": os.urandom(6).hex(),
    }
    data = json.dumps(payload, separators=(",", ":")).encode()
    data_hex = data.hex()
    mac = hmac.new(secret, data_hex.encode(), hashlib.sha256).hexdigest()[:16]
    return data_hex + "." + mac


def _verify_token(token: str, secret: bytes) -> dict | None:
    idx = token.rfind(".")
    if idx <= 0:
        return None
    data_hex = token[:idx]
    mac = token[idx + 1:]
    expected = hmac.new(secret, data_hex.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(mac, expected):
        return None

    try:
        payload = json.loads(bytes.fromhex(data_hex).decode())
    except Exception:
        return None

    if time.time() > payload["exp"]:
        return None

    # 校验 user_version：与 DB 当前值比对，不匹配则 token 已被改密/登出作废
    user = users_store.get(payload["uname"])
    if not user or user.user_version != payload["uv"]:
        return None

    return payload


def _validate_password_strength(password: str) -> str | None:
    if len(password) < 8:
        return "密码长度至少 8 位"
    if not re.search(r"[a-zA-Z]", password):
        return "密码必须包含字母"
    if not re.search(r"[0-9]", password):
        return "密码必须包含数字"
    return None


def _get_user_from_cookie(request: Request) -> dict | None:
    token = request.cookies.get("auth_token")
    if not token:
        return None
    info = _get_or_create_secret()
    return _verify_token(token, info["secret"])


def _get_user_from_bearer(request: Request) -> dict | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:]
    info = _get_or_create_secret()
    return _verify_token(token, info["secret"])


def _extract_api_token(request: Request) -> str | None:
    """从 X-Api-Key 或 Authorization: Bearer 中提取 token。"""
    api_key = request.headers.get("X-Api-Key") or request.headers.get("x-api-key")
    if api_key:
        return api_key
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    return None


# ============================================================
# 依赖：获取当前用户
# ============================================================

def get_current_user(request: Request) -> dict | None:
    """同步获取当前用户（用于中间件）。"""
    return _get_user_from_cookie(request) or _get_user_from_bearer(request)


# ============================================================
# 中间件依赖
# ============================================================

async def _require_web_auth(request: Request) -> dict:
    """Web Cookie 认证——未登录返回 401。"""
    user = _get_user_from_cookie(request)
    if not user:
        if request.url.path.startswith("/admin/") or request.url.path.startswith("/api/inner/"):
            raise HTTPException(status_code=401, detail={"error": "请先登录"})
        raise HTTPException(status_code=401, detail={"error": "请先登录"})
    return user


async def _require_api_auth(request: Request) -> dict:
    """API 认证——Cookie 或 Bearer。"""
    user = _get_user_from_cookie(request)
    if not user:
        user = _get_user_from_bearer(request)
    if not user:
        raise HTTPException(status_code=401, detail={"error": "请先登录"})
    return user


async def _require_admin(request: Request) -> dict:
    """Admin 权限检查。"""
    user = await _require_api_auth(request)
    if user.get("role") != 1:
        raise HTTPException(status_code=403, detail={"error": "需要管理员权限"})
    return user


def build_api_token_dependency(static_api_token: str):
    """
    对外接口鉴权依赖。
    优先级：DB 中 api_auth_enabled === '1' → DB token；否则 fallback 到静态 token。
    管理后台（系统设置）覆盖 cfg.yml 静态值。
    支持 X-Api-Key 和 Authorization: Bearer 两种方式。
    """

    async def verify(request: Request):
        db_enabled = config_store.get("api_auth_enabled")
        db_token = config_store.get("api_auth_token")

        # DB 启用优先
        if db_enabled == "1" and db_token:
            token = _extract_api_token(request)
            if not token or token != db_token:
                resp = {
                    "code": 1003,
                    "message": "Unauthorized: 无效或缺失的 X-Api-Key / Authorization Bearer Token",
                    "task_id": "",
                    "linkid": "",
                }
                log.warning("api_auth_failed path=%s", request.url.path)
                raise HTTPException(status_code=401, detail=resp)
            return

        # Fallback: cfg.yml 静态 token
        if static_api_token:
            token = _extract_api_token(request)
            if not token or token != static_api_token:
                resp = {
                    "code": 1003,
                    "message": "Unauthorized: 无效或缺失的 X-Api-Key / Authorization Bearer Token",
                    "task_id": "",
                    "linkid": "",
                }
                log.warning("api_auth_failed path=%s", request.url.path)
                raise HTTPException(status_code=401, detail=resp)

        # 都没有 → 放行
    return verify


# ============================================================
# 路由
# ============================================================

@router.post("/api/login")
async def login(request: Request, response: Response):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "请求体无效"})

    user_name = body.get("user_name", "")
    user_pwd = body.get("user_pwd", "")
    if not user_name or not user_pwd:
        raise HTTPException(status_code=400, detail={"error": "用户名和密码不能为空"})

    user = users_store.get(user_name)
    if not user:
        log.warning("auth_login_user_not_found username=%s", user_name)
        raise HTTPException(status_code=401, detail={"error": "用户名或密码错误"})

    if not compare_password(user_pwd, user.password_hash):
        log.warning("auth_login_wrong_password username=%s", user_name)
        raise HTTPException(status_code=401, detail={"error": "用户名或密码错误"})

    # admin 首次登录：2h 内未修改则初始密码失效
    if user.must_change_pwd:
        created_sec = user.created_ts / 1000 if user.created_ts else None
        if created_sec is None and user.created_at:
            try:
                from datetime import datetime
                created_dt = datetime.fromisoformat(user.created_at)
                created_sec = created_dt.timestamp()
            except (ValueError, OSError):
                pass
        if created_sec and time.time() - created_sec > MUST_CHANGE_PWD_WINDOW:
            log.warning("auth_must_change_pwd_expired username=%s", user_name)
            raise HTTPException(status_code=403, detail={"error": "初始密码已过期（2小时内未修改），请联系管理员重置"})

    info = _get_or_create_secret()
    token = _sign_token(user.id, user.username, user.role, user.user_version, info["secret"], info["temporary"])

    ttl = SHORT_TTL if info["temporary"] else LONG_TTL
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        max_age=ttl,
        path="/",
        samesite="lax",
    )

    log.info("user_login user=%s", user.username)

    return {
        "status": "ok",
        "token": token,
        "must_change_pwd": bool(user.must_change_pwd),
        "user": {"id": user.id, "username": user.username, "role": user.role},
    }


@router.post("/api/logout")
async def logout(request: Request, response: Response):
    token_payload = _get_user_from_cookie(request) or _get_user_from_bearer(request)
    if token_payload:
        users_store.increment_user_version(token_payload["uname"])
        log.info("user_logout_and_tokens_invalidated user=%s", token_payload["uname"])
    response.delete_cookie("auth_token", path="/")
    return {"status": "ok"}


@router.get("/api/inner/whoami")
async def whoami(request: Request):
    """前端探活，无副作用。"""
    user = await _require_api_auth(request)
    return {"code": 0, "data": {"id": user["uid"], "username": user["uname"], "role": user["role"]}}


@router.put("/api/inner/user/password")
async def change_password(request: Request):
    user_payload = await _require_api_auth(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "参数错误"})

    old_pwd = body.get("old_pwd", "")
    new_pwd = body.get("new_pwd", "")
    if not new_pwd:
        raise HTTPException(status_code=400, detail={"error": "参数错误"})

    strength_err = _validate_password_strength(new_pwd)
    if strength_err:
        raise HTTPException(status_code=400, detail={"error": f"新密码: {strength_err}"})

    user = users_store.get(user_payload["uname"])
    if not user:
        raise HTTPException(status_code=404, detail={"error": "用户不存在"})

    # 验证旧密码（跳过 admin 首次修改的场景）
    if old_pwd:
        if not compare_password(old_pwd, user.password_hash):
            raise HTTPException(status_code=400, detail={"error": "旧密码错误"})

    new_hash = hash_password(new_pwd)
    users_store.update_password(user_payload["uname"], new_hash)
    users_store.clear_must_change_pwd(user_payload["uname"])

    # admin 改密后持久化密钥
    persist_secret()

    log.info("user_password_changed user=%s", user_payload["uname"])
    return {"status": "ok"}