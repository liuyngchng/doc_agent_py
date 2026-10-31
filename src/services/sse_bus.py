"""
轻量 SSE 事件总线 —— 连接 SDK 流式消息与前端 EventSource

sdk_runner 每收到一条消息就 publish，admin 路由的 SSE 端点 subscribe 消费。
基于 asyncio.Queue：每个订阅者持有一个队列，publish 时向该 session 的所有队列投递。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# session_id -> set[Queue]（每个 SSE 连接一个 Queue）
_channels: dict[str, set[asyncio.Queue]] = {}


def subscribe(session_id: str) -> asyncio.Queue:
    """注册一个订阅者，返回其专属消息队列。"""
    q: asyncio.Queue = asyncio.Queue()
    s = _channels.get(session_id)
    if s is None:
        s = set()
        _channels[session_id] = s
    s.add(q)
    return q


def unsubscribe(session_id: str, q: asyncio.Queue) -> None:
    """注销订阅者并清理队列。"""
    s = _channels.get(session_id)
    if s:
        s.discard(q)
        if not s:
            _channels.pop(session_id, None)


def publish(session_id: str, message: Any) -> None:
    """向指定 session 的所有订阅者投递消息。"""
    s = _channels.get(session_id)
    if not s:
        return
    for q in s:
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass  # 订阅者处理不过来则丢弃，避免阻塞 SDK 流


def sse_encode(message: Any) -> bytes:
    """把消息编码为 SSE data 帧。"""
    return f"data: {json.dumps(message, ensure_ascii=False, default=str)}\n\n".encode("utf-8")