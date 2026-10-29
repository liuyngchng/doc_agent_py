"""
认证路由 — 对应 TypeScript 版 `routes/auth.ts`。

Cookie/Token 双模认证，admin 首次登录强制改密。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Any

from fastapi import APIRouter, Request, HTTPException
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


def _persist_secret() -> None:
    info = _get_or_create_secret()
    if info["temporary"]:
        config_store.set("token_secret", info["secret"].hex(), "admin 改密后持久化的 token 签名密钥")
        global _cached_secret
        _cached_secret = {"secret": info["secret"], "temporary": False}
        log.info("auth_key_persisted — admin 修改密码后，临时密钥已持久化到数据库")


def _sign_token(user_id: int, username: str, role: int, secret: bytes, temporary: bool) -> str:
    ttl = SHORT_TTL if temporary else LONG_TTL
    payload = {
        "uid": user_id,
        "uname": username,
        "role": role,
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


# ============================================================
# 依赖：获取当前用户
# ============================================================

async def _require_web_auth(request: Request) -> dict:
    """Web Cookie 认证——未登录返回 401 或重定向。"""
    user = _get_user_from_cookie(request)
    if not user:
        if request.url.path.startswith("/admin/") or request.url.path.startswith("/api/inner/"):
            raise HTTPException(status_code=401, detail="请先登录")
        raise HTTPException(status_code=401, detail="请先登录")
    return user


async def _require_api_auth(request: Request) -> dict:
    """API 认证——Cookie 或 Bearer。"""
    user = _get_user_from_cookie(request)
    if not user:
        user = _get_user_from_bearer(request)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


async def _require_admin(request: Request) -> dict:
    """Admin 权限检查。"""
    user = await _require_api_auth(request)
    if user.get("role") != 1:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def get_current_user(request: Request) -> dict | None:
    """同步获取当前用户（用于中间件）。"""
    return _get_user_from_cookie(request) or _get_user_from_bearer(request)


def build_api_token_dependency(api_token: str):
    """对外接口鉴权依赖（Authorization: Bearer <api_token>）。

    token 由 cfg.yml 的 sys.api_token 配置，留空则不校验（开发便利）。
    """

    async def verify(request: Request):
        if not api_token:
            return
        header = request.headers.get("Authorization", "")
        if header != f"Bearer {api_token}":
            raise HTTPException(
                status_code=401,
                detail={
                    "code": 1003,
                    "message": "Unauthorized: 无效或缺失的 Authorization Bearer Token",
                    "task_id": "",
                    "linkid": "",
                },
            )
    return verify


# ============================================================
# 路由
# ============================================================

@router.post("/api/login")
async def login(request: Request, response: Response):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体无效")

    user_name = body.get("user_name", "")
    user_pwd = body.get("user_pwd", "")
    if not user_name or not user_pwd:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")

    user = users_store.get(user_name)
    if not user:
        log.warning("auth_login_user_not_found username=%s", user_name)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not compare_password(user_pwd, user.password_hash):
        log.warning("auth_login_wrong_password username=%s", user_name)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # admin 首次登录：2h 内未修改则初始密码失效
    if user.must_change_pwd and user.created_at:
        try:
            from datetime import datetime, timezone
            created_dt = datetime.fromisoformat(user.created_at)
            created_ts = created_dt.timestamp()
            if time.time() - created_ts > MUST_CHANGE_PWD_WINDOW:
                log.warning("auth_must_change_pwd_expired username=%s", user_name)
                raise HTTPException(status_code=403, detail="初始密码已过期（2小时内未修改），请联系管理员重置")
        except Exception:
            pass

    info = _get_or_create_secret()
    token = _sign_token(user.id, user.username, user.role, info["secret"], info["temporary"])

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
async def logout(response: Response):
    response.delete_cookie("auth_token", path="/")
    return {"status": "ok"}


@router.put("/api/inner/user/password")
async def change_password(request: Request):
    user_payload = await _require_api_auth(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="参数错误")

    old_pwd = body.get("old_pwd", "")
    new_pwd = body.get("new_pwd", "")
    if not new_pwd:
        raise HTTPException(status_code=400, detail="参数错误")

    strength_err = _validate_password_strength(new_pwd)
    if strength_err:
        raise HTTPException(status_code=400, detail=f"新密码: {strength_err}")

    user = users_store.get(user_payload["uname"])
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 验证旧密码（跳过 admin 首次修改的场景）
    if old_pwd:
        if not compare_password(old_pwd, user.password_hash):
            raise HTTPException(status_code=400, detail="旧密码错误")

    new_hash = hash_password(new_pwd)
    users_store.update_password(user_payload["uname"], new_hash)
    users_store.clear_must_change_pwd(user_payload["uname"])

    # admin 改密后持久化密钥
    _persist_secret()

    log.info("user_password_changed user=%s", user_payload["uname"])
    return {"status": "ok"}
