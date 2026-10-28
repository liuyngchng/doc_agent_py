"""
类型定义 — 与 TypeScript 版 `types.ts` 完全对应。
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
    created_at: str
    updated_at: str


# ============================================================
# 任务状态
# ============================================================


class TaskStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    success = "success"
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
    path: str
    interface_id: str | None
    status: TaskStatus
    request_body: str
    prompt: str
    sdk_output: str | None
    sdk_trace: str | None
    callback_body: str | None
    callback_url: str
    error_message: str | None
    created_at: str
    finished_at: str | None


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
    created_at: str