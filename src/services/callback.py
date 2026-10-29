"""
回调投递 + 重试 — 对应 TypeScript 版 `callback.ts`。

向 callback_url 发起 POST，失败自动重试（默认 3 次，指数退避）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx

from ..config import Config
from ..logger import get_logger

log = get_logger()


@dataclass
class CallbackResult:
    success: bool
    body: str
    retry_count: int
    error_msg: str | None = None


async def _post_once(url: str, payload: str, token: str | None = None) -> bool:
    """发送单次回调 POST。"""
    headers: dict[str, str] = {
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            verify=False,  # 对应 Node 版不验证证书——自签名环境
        ) as client:
            resp = await client.post(url, content=payload, headers=headers)
        ok = 200 <= resp.status_code < 300
        log.info("callback_response url=%s status=%d ok=%s", url, resp.status_code, ok)
        return ok
    except Exception as exc:
        log.warning("callback_request_error url=%s error=%s", url, exc)
        return False


async def deliver_callback(
    callback_url: str,
    payload: dict,
    cfg: Config,
) -> CallbackResult:
    """投递回调，重试最多 N 次（指数退避）。"""
    max_retries = cfg.sys.callback_retries
    base_interval = cfg.sys.callback_retry_interval_sec
    token = cfg.sys.file_url_token or None
    body_str = json.dumps(payload, ensure_ascii=False, default=str)

    log.info("callback_start task_id=%s url=%s", payload.get("task_id", ""), callback_url)

    retry_count = 0

    for attempt in range(max_retries + 1):
        if attempt > 0:
            retry_count = attempt
            delay = base_interval * (2 ** (attempt - 1))
            log.warning(
                "callback_retry task_id=%s attempt=%d delay_sec=%d",
                payload.get("task_id", ""), attempt, delay,
            )
            await asyncio.sleep(delay)

        ok = await _post_once(callback_url, body_str, token)
        if ok:
            log.info(
                "callback_success task_id=%s retries=%d",
                payload.get("task_id", ""), retry_count,
            )
            return CallbackResult(success=True, body=body_str, retry_count=retry_count)

    err_msg = f"回调失败，已重试 {max_retries} 次"
    log.error(
        "callback_all_retries_failed task_id=%s retries=%d",
        payload.get("task_id", ""), max_retries,
    )
    return CallbackResult(
        success=False,
        body=body_str,
        retry_count=max_retries,
        error_msg=err_msg,
    )
