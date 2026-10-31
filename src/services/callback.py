"""
回调投递 + 重试 — 向 callback_url 发起 POST，失败自动重试（默认 3 次，指数退避）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Callable, Awaitable

import httpx

from ..config import Config
from ..logger import get_logger
from ..time import fmt_local_ts, now_readable
from ..types import StreamEntry

log = get_logger()


@dataclass
class CallbackResult:
    success: bool
    body: str
    response_body: str = ""
    last_status_code: int | None = None
    retry_count: int = 0
    error_msg: str | None = None


def _ts() -> str:
    return now_readable()


async def _post_once(url: str, payload: str, token: str | None = None) -> dict:
    """发送单次回调 POST。返回 {ok, status_code, response_body, error}。"""
    headers: dict[str, str] = {
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            verify=False,  # 自签名环境
        ) as client:
            resp = await client.post(url, content=payload, headers=headers)
        ok = 200 <= resp.status_code < 300
        response_body = resp.text
        log.debug(
            "callback_response url=%s status_code=%d ok=%s",
            url, resp.status_code, ok,
        )
        return {"ok": ok, "status_code": resp.status_code, "response_body": response_body, "error": None}
    except Exception as exc:
        err_msg = str(exc)
        log.debug("callback_request_error url=%s error=%s", url, err_msg)
        return {"ok": False, "status_code": None, "response_body": "", "error": err_msg}


async def deliver_callback(
    callback_url: str,
    payload: dict,
    cfg: Config,
    on_stream: Callable[[list[StreamEntry]], Awaitable[None]] | None = None,
) -> CallbackResult:
    """投递回调，重试最多 N 次（指数退避）。"""
    max_retries = cfg.sys.callback_retries
    base_interval = cfg.sys.callback_retry_interval_sec
    token = cfg.sys.file_url_token or None
    body_str = json.dumps(payload, ensure_ascii=False, default=str)

    # 生成 curl 命令
    body_esc = body_str.replace("'", "'\\''")
    auth_header = f"-H 'Authorization: Bearer {token}'" if token else "-H 'Authorization: Bearer sk-***'"
    curl_cmd = f"curl -k --noproxy '*' -w'\\n' -H 'Content-Type: application/json' {auth_header} -d '{body_esc}' {callback_url}"

    if on_stream:
        await on_stream([StreamEntry(
            ts=_ts(),
            type="system",
            text=f"开始回调 POST {callback_url}",
            tool_name="callback",
            tool_input=curl_cmd,
        )])

    # 打印 request（包含 curl）
    task_id = payload.get("task_id", "")
    log.info("callback_request task_id=%s url=%s curl=%s", task_id, callback_url or "(空)", curl_cmd)
    log.debug("callback_request_payload task_id=%s payload=%s", task_id, payload)

    retry_count = 0
    last_response_body = ""
    last_status_code: int | None = None
    last_error: str | None = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            retry_count = attempt
            delay = base_interval * (2 ** (attempt - 1))
            if on_stream:
                await on_stream([StreamEntry(
                    ts=_ts(),
                    type="system",
                    text=f"回调重试 #{attempt}，{delay}s 后重试",
                )])
            log.warning(
                "callback_retry task_id=%s attempt=%d delay_sec=%d last_status_code=%s last_error=%s last_response=%s",
                task_id, attempt, delay, last_status_code, last_error,
                last_response_body[:500] if last_response_body else "",
            )
            await asyncio.sleep(delay)

        result = await _post_once(callback_url, body_str, token)
        last_response_body = result["response_body"]
        last_status_code = result["status_code"]
        last_error = result.get("error")  # type: ignore[assignment]

        if result["ok"]:
            if on_stream:
                await on_stream([StreamEntry(
                    ts=_ts(),
                    type="tool_result",
                    text=f"回调成功 (HTTP {result['status_code']})",
                    tool_output=last_response_body,
                )])
            log.info(
                "callback_success task_id=%s retries=%d status_code=%s",
                task_id, retry_count, result["status_code"],
            )
            return CallbackResult(
                success=True,
                body=body_str,
                response_body=result["response_body"],
                last_status_code=result["status_code"],
                retry_count=retry_count,
            )

    http_info = f"HTTP {last_status_code}" if last_status_code else ""
    parts = [http_info, last_error] if http_info and last_error else [http_info or last_error or ""]
    err_info = f" ({', '.join(p for p in parts if p)})" if any(parts) else ""
    err_msg = f"回调失败{err_info}，已重试 {max_retries} 次"

    if on_stream:
        await on_stream([StreamEntry(
            ts=_ts(),
            type="error",
            text=err_msg,
            tool_output=last_response_body,
        )])

    log.error(
        "callback_all_retries_failed task_id=%s retries=%d last_status_code=%s last_error=%s",
        task_id, max_retries, last_status_code, last_error,
    )
    return CallbackResult(
        success=False,
        body=body_str,
        response_body=last_response_body,
        last_status_code=last_status_code,
        retry_count=max_retries,
        error_msg=err_msg,
    )