"""
Claude Code Agent SDK 调用封装 — 对应 TypeScript 版 `sdk-runner.ts`。

每个任务一个独立工作目录，写入 CLAUDE.md 行为规范，
然后通过 SDK 的 query() 执行。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)

from ..config import Config
from ..logger import get_logger

log = get_logger()


def _build_env(cfg: Config) -> dict[str, str | None]:
    """根据 LLM 配置构建子进程环境变量。

    Python SDK 无 per-query env 参数（与 TS 版的不同之处），
    通过 os.environ 在每次 run_agent 前覆盖。
    """
    env: dict[str, str | None] = {}
    llm = cfg.llm

    env["ANTHROPIC_BASE_URL"] = llm.base_url

    if llm.auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = llm.auth_token
    else:
        env["ANTHROPIC_AUTH_TOKEN"] = ""

    if llm.model:
        env["ANTHROPIC_MODEL"] = llm.model
    if llm.opus_model:
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = llm.opus_model
    if llm.sonnet_model:
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = llm.sonnet_model
    if llm.haiku_model:
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = llm.haiku_model
    if llm.subagent_model:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = llm.subagent_model
    if llm.effort_level:
        env["CLAUDE_CODE_EFFORT_LEVEL"] = llm.effort_level
    if llm.timeout_ms:
        env["API_TIMEOUT_MS"] = str(llm.timeout_ms)

    # 文件下载 Token（CLAUDE.md 中通过 $FILE_URL_TOKEN 引用）
    if cfg.sys.file_url_token:
        env["FILE_URL_TOKEN"] = cfg.sys.file_url_token
    else:
        env["FILE_URL_TOKEN"] = ""

    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = "0"

    return env


def _build_claude_md(doc_util_url: str, renew_file_url_api: str) -> str:
    wps_section = ""
    if renew_file_url_api:
        wps_section = f"""
## 文件下载回退（WPS 接口）

如果通过请求中的 `file_url` 直接下载文件失败（超时、403、连接被拒等），
可以尝试通过 WPS 接口用 `file_id` 重新获取新的下载链接。

接口令牌通过环境变量 `FILE_URL_TOKEN` 提供，**不要在日志或屏幕上输出此 Token**。

```bash
curl -s -k --noproxy '*' -X POST '{renew_file_url_api}' \\
  -H 'Content-Type: application/json' \\
  -H "Authorization: Bearer $FILE_URL_TOKEN" \\
  -d '{{"fileId": "<文件ID>", "getFileUrlType": "download"}}' | jq -r '.data'
```

步骤：
1. 从请求 JSON 的 `files[].file_id` 字段获取文件 ID
2. 用上述命令获取新下载 URL（`jq -r '.data'` 返回的就是 file_url）
3. 用新的 URL 重新 curl 下载文件
4. 如果仍然失败，标记 `need_human: true` 并在 `fail_reason` 中说明原因
"""

    return f"""# doc_agent_py 工作区

你是电子公文处理智能体，运行在这个临时工作目录中。

## 文件下载
- 对方传递的 JSON 中的文件 URL 已开放白名单，无需认证
- 如果文件 URL 需要认证，可用环境变量 `FILE_URL_TOKEN`（Bearer Token）下载：
  `curl -s -k --noproxy '*' -H "Authorization: Bearer $FILE_URL_TOKEN" '<file_url>' -o 文件名`
- 用 curl 下载文件到当前工作目录
{wps_section}
## 文档解析服务

同网络下有一个文档解析服务，可将 PDF / DOCX / .doc 转为纯文本，同时自动检测印章。

```bash
curl -s -k --noproxy '*' -X POST {doc_util_url}/extract/txt \\
  -F "file=@文件名" | jq '.'
```

返回 JSON:
- `text`: 提取的全部文本
- `has_seal`: 是否检测到印章 (true/false)
- `seal_text`: 印章文字列表，如 ["北京XX科技有限公司", "财务专用章"]
- `ocr_confidence`: OCR 置信度 0.0–1.0 (1.0=纯文字提取，无需 OCR)

示例:
```bash
curl -s -k --noproxy '*' -X POST {doc_util_url}/extract/txt \\
  -F "file=@document.pdf" | jq -r '.text'
```

注意事项:
- 该服务使用自签名 HTTPS 证书，务必带 `-k` 跳过证书校验
- 用 `--noproxy '*'` 绕过代理直连
- 对下载的每个文件都调用此服务提取文本
- 文件大小上限 64MB

## 行为规范
1. 所有文本内容（fail_reason、opinion_text、desc 等）必须使用简体中文
2. 严格按提示词要求的 JSON Schema 输出
3. 最终结果用 ```json 代码块包裹
4. json 代码块之外不要输出任何其他内容
5. 如果下载失败或文件无法识别，在 JSON 中标记 need_human: true
"""


@dataclass
class SdkResult:
    success: bool
    raw_output: str
    trace: str
    error_message: str | None = None
    duration_ms: int = 0


async def run_agent(
    task_id: str,
    rendered_prompt: str,
    cfg: Config,
) -> SdkResult:
    """运行 Claude Agent SDK 处理一个任务。"""
    work_dir = Path(cfg.sys.data_dir) / "tasks" / task_id
    work_dir.mkdir(parents=True, exist_ok=True)

    claude_md_path = work_dir / "CLAUDE.md"
    claude_md_path.write_text(
        _build_claude_md(cfg.sys.doc_util_url, cfg.sys.renew_file_url_api),
        encoding="utf-8",
    )

    log.info("sdk_run_start task_id=%s", task_id)

    start = time.monotonic()
    full_output = ""
    trace_messages: list[Any] = []

    try:
        # Python SDK 无 per-query env 参数（TS 版有），
        # 通过 os.environ 应用 LLM 配置（所有任务共享同一 LLM，幂等覆盖无副作用）。
        env_overrides = _build_env(cfg)
        os.environ.update({k: v for k, v in env_overrides.items() if v})

        options = ClaudeAgentOptions(
            model=cfg.llm.model or cfg.sys.model,
            cwd=str(work_dir),
            allowed_tools=["Bash", "Read", "Write", "Glob", "Grep", "WebSearch", "WebFetch"],
            permission_mode="dontAsk",        # TS 版用 bypassPermissions，Python 版对应 dontAsk
            system_prompt=_build_claude_md(cfg.sys.doc_util_url, cfg.sys.renew_file_url_api),
            max_buffer_size=10 * 1024 * 1024,  # 10MB for images
        )

        async for message in query(prompt=rendered_prompt, options=options):
            trace_messages.append(message)
            log.debug("sdk_stream_msg task_id=%s msg_type=%s", task_id, type(message).__name__)

            if isinstance(message, AssistantMessage):
                if message.content:
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            full_output += block.text

        duration_ms = int((time.monotonic() - start) * 1000)
        trace = json.dumps(trace_messages, default=str, ensure_ascii=False)
        log.info(
            "sdk_run_complete task_id=%s duration_ms=%d output_len=%d trace_len=%d",
            task_id, duration_ms, len(full_output), len(trace),
        )

        return SdkResult(success=True, raw_output=full_output, trace=trace, duration_ms=duration_ms)

    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        message = str(exc)
        trace = json.dumps(trace_messages, default=str, ensure_ascii=False)
        log.error(
            "sdk_run_error task_id=%s duration_ms=%d error=%s",
            task_id, duration_ms, message,
        )
        return SdkResult(
            success=False,
            raw_output=full_output,
            trace=trace,
            error_message=message,
            duration_ms=duration_ms,
        )
