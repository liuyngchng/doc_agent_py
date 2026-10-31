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
class StreamEntry:
    """SDK 流式输出中的一条可读记录，实时追加到 DB。"""
    ts: str          # YYYY-MM-DD HH:MM:ss.SSS
    type: str        # 'text' | 'tool' | 'tool_result' | 'system' | 'error'
    text: str        # 人类可读摘要
    tool_name: str | None = None
    tool_input: str | None = None   # JSON-stringified tool input
    tool_output: str | None = None  # tool result summary (truncated)


@dataclass
class TaskLog:
    task_id: str
    linkid: str
    path: str
    interface_id: str | None
    status: TaskStatus
    request_body: str
    request_response: str | None     # 我方对请求的同步响应
    prompt: str
    sdk_output: str | None
    sdk_trace: str | None
    sdk_stream: str | None
    callback_body: str | None
    callback_response: str | None    # 对方对回调的响应
    callback_url: str
    error_message: str | None
    created_at: str           # YYYY-MM-DD HH:MM:ss.SSS 本地时间
    finished_at: str | None
    created_ts: int | None    # Unix 毫秒时间戳（检索用）
    finished_ts: int | None


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