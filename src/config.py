"""
配置加载 — 支持 YAML 文件 + 环境变量覆盖。

优先级：环境变量 > cfg.yml > 默认值。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---- 默认值 ----
DATA_DIR = os.environ.get("DATA_DIR", str(Path.cwd() / "data"))


@dataclass
class ServerConfig:
    port: int = 20001
    debug: bool = False
    cert_file: str = ""
    key_file: str = ""
    self_ip: str = ""   # 对外服务 IP，留空自动探测


@dataclass
class SysConfig:
    max_concurrent_tasks: int = 5
    max_pending_tasks: int = 10
    callback_retries: int = 3
    callback_retry_interval_sec: int = 2
    retention_days: int = 30
    data_dir: str = DATA_DIR
    doc_util_url: str = "https://doc_util_server:8443"
    renew_file_url_api: str = ""
    file_url_token: str = ""
    api_token: str = ""


@dataclass
class LlmConfig:
    base_url: str = ""
    auth_token: str = ""
    model: str = "deepseek-v4-pro"
    opus_model: str = ""
    sonnet_model: str = ""
    haiku_model: str = ""
    subagent_model: str = ""
    effort_level: str = ""
    timeout_ms: int = 600000
    disable_nonessential_traffic: bool = True
    attribution_header: bool = False


@dataclass
class DocAuditConfig:
    api_url: str = ""
    token: str = ""


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    sys: SysConfig = field(default_factory=SysConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    doc_audit: DocAuditConfig = field(default_factory=DocAuditConfig)


def _env(k: str, default: str) -> str:
    return os.environ.get(k, default)


def _env_int(k: str, default: int) -> int:
    v = os.environ.get(k)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _env_bool(k: str, default: bool) -> bool:
    v = os.environ.get(k)
    if v is None:
        return default
    return v in ("1", "true", "True", "yes")


def _load_cfg_file() -> dict[str, Any]:
    """从 data/cfg.yml 加载 YAML 配置。"""
    paths = [
        Path(DATA_DIR) / "cfg.yml",
        Path.cwd() / "data" / "cfg.yml",
    ]
    for p in paths:
        if p.exists():
            try:
                raw = p.read_text(encoding="utf-8")
                return yaml.safe_load(raw) or {}
            except Exception:
                print("config: 解析 cfg.yml 失败，使用默认值", file=sys.stderr)
    return {}


_cfg = _load_cfg_file()


def load_config() -> Config:
    s = _cfg.get("server") or {}
    y = _cfg.get("sys") or {}
    l = _cfg.get("llm") or {}
    da = _cfg.get("doc_audit") or {}

    llm_base_url = _env("ANTHROPIC_BASE_URL", l.get("base_url", ""))
    if not llm_base_url:
        print(
            "[config] 缺少 llm.base_url（或环境变量 ANTHROPIC_BASE_URL）。\n"
            "         必须指向 Anthropic 协议兼容网关（如 api_adapter_g）。\n"
            "         请在 data/cfg.yml 中填写 llm.base_url。",
            file=sys.stderr,
        )
        sys.exit(1)

    return Config(
        server=ServerConfig(
            port=_env_int("SERVER_PORT", s.get("port", 20001)),
            debug=_env_bool("DEBUG", s.get("debug", False)),
            cert_file=_env("CERT_FILE", s.get("cert_file", "")),
            key_file=_env("KEY_FILE", s.get("key_file", "")),
            self_ip=_env("SELF_IP", s.get("self_ip", "")),
        ),
        sys=SysConfig(
            max_concurrent_tasks=_env_int("MAX_CONCURRENT_TASKS", y.get("max_concurrent_tasks", 5)),
            max_pending_tasks=_env_int("MAX_PENDING_TASKS", y.get("max_pending_tasks", 10)),
            callback_retries=_env_int("CALLBACK_RETRIES", y.get("callback_retries", 3)),
            callback_retry_interval_sec=_env_int(
                "CALLBACK_RETRY_INTERVAL_SEC", y.get("callback_retry_interval_sec", 2)
            ),
            retention_days=_env_int("RETENTION_DAYS", y.get("retention_days", 30)),
            data_dir=_env("DATA_DIR", y.get("data_dir", DATA_DIR)),
            doc_util_url=_env("DOC_UTIL_URL", y.get("doc_util_url", "https://doc_util_server:8443")),
            renew_file_url_api=_env("RENEW_FILE_URL_API", y.get("renew_file_url_api", "")),
            file_url_token=_env("FILE_URL_TOKEN", y.get("file_url_token", "")),
            api_token=_env("API_TOKEN", y.get("api_token", "")),
        ),
        llm=LlmConfig(
            base_url=llm_base_url,
            auth_token=_env("ANTHROPIC_AUTH_TOKEN", l.get("auth_token", "")),
            model=_env("ANTHROPIC_MODEL", l.get("model", "deepseek-v4-pro")),
            opus_model=_env("ANTHROPIC_DEFAULT_OPUS_MODEL", l.get("opus_model", "")),
            sonnet_model=_env("ANTHROPIC_DEFAULT_SONNET_MODEL", l.get("sonnet_model", "")),
            haiku_model=_env("ANTHROPIC_DEFAULT_HAIKU_MODEL", l.get("haiku_model", "")),
            subagent_model=_env("CLAUDE_CODE_SUBAGENT_MODEL", l.get("subagent_model", "")),
            effort_level=_env("CLAUDE_CODE_EFFORT_LEVEL", l.get("effort_level", "")),
            timeout_ms=_env_int("API_TIMEOUT_MS", l.get("timeout_ms", 600000)),
            disable_nonessential_traffic=_env_bool(
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                l.get("disable_nonessential_traffic", True),
            ),
            attribution_header=_env_bool(
                "CLAUDE_CODE_ATTRIBUTION_HEADER", l.get("attribution_header", False)
            ),
        ),
        doc_audit=DocAuditConfig(
            api_url=_env("DOC_AUDIT_URL", da.get("api_url", "")),
            token=_env("DOC_AUDIT_TOKEN", da.get("token", "")),
        ),
    )


def ensure_dirs(cfg: Config) -> None:
    """确保运行时目录存在。"""
    Path(cfg.sys.data_dir, "tasks").mkdir(parents=True, exist_ok=True)
    Path(cfg.sys.data_dir, "logs").mkdir(parents=True, exist_ok=True)