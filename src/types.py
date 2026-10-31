"""
类型定义 — 数据模型、请求/响应类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ============================================================
# 接口（path + 提示词模板）
# ============================================================


@dataclass
class AgentInterface:
    id: str
    name: str
    path: str
    prompt: str
    created_at: str       # YYYY-MM-DD HH:MM:ss.SSS 本地时间
    updated_at: str
    created_ts: int | None = None  # Unix 毫秒时间戳（检索用）
    updated_ts: int | None = None


# ============================================================
# 任务状态
# ============================================================


class TaskStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    failed = "failed"
    completed = "completed"
    callback_fail = "callback_fail"
    cancelled = "cancelled"


# ============================================================
# 任务日志
# ============================================================


@dataclass
class TaskLog:
    task_id: str
    linkid: str
    session_id: str | None        # 关联的 Claude Agent SDK session（resume 用）
    path: str
    interface_id: str | None
    status: TaskStatus
    request_body: str
    request_response: str | None  # 我方对请求的同步响应
    prompt: str
    sdk_output: str | None        # Agent 最终输出（业务产出，回调 data）
    callback_body: str | None
    callback_response: str | None  # 对方对回调的响应
    callback_url: str
    error_message: str | None
    created_at: str           # YYYY-MM-DD HH:MM:ss.SSS 本地时间
    finished_at: str | None
    created_ts: int | None    # Unix 毫秒时间戳（检索用）
    finished_ts: int | None


# ============================================================
# Session（公文 ↔ Claude Agent SDK 会话映射）
# ============================================================


class SessionStatus(StrEnum):
    active = "active"
    closed = "closed"


@dataclass
class Session:
    session_id: str
    linkid: str
    status: SessionStatus
    created_at: str
    updated_at: str
    created_ts: int | None = None
    updated_ts: int | None = None


# ============================================================
# API 响应信封
# ============================================================


@dataclass
class ApiResponse:
    code: int
    timestamp: str
    message: str
    task_id: str
    linkid: str
    data: Any = field(default_factory=dict)


# ============================================================
# 请求体（最小约束）
# ============================================================


class ProcessRequest(dict):
    """自由 JSON，只强制 linkid + callback_url。"""

    @property
    def linkid(self) -> str:
        return self.get("linkid", "")

    @property
    def callback_url(self) -> str:
        return self.get("callback_url", "")


# ============================================================
# 任务队列项
# ============================================================


@dataclass
class QueueItem:
    task_id: str
    path: str
    iface: AgentInterface
    body: ProcessRequest


# ============================================================
# 用户
# ============================================================


@dataclass
class User:
    id: int
    username: str
    password_hash: str
    role: int  # 1 = admin, 0 = normal
    note: str
    must_change_pwd: bool
    user_version: int = 1     # 递增可使该用户所有旧 token 失效（改密/登出时 +1）
    created_at: str = ""
    created_ts: int | None = None  # Unix 毫秒时间戳（检索用）