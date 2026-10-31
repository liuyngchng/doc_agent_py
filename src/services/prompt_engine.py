"""
提示词渲染引擎 — 支持 `{{request.body}}` `{{request.linkid}}` `{{request.xxx.yyy}}` 等多层级占位符。
"""

from __future__ import annotations

import json
import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{\{(request\.[^}]+)\}\}")


def _deep_get(obj: Any, path: str) -> Any:
    """从对象中按点号路径取值。"""
    parts = path.split(".")
    cur: Any = obj
    for p in parts:
        if cur is None or not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def render_prompt(template: str, request_body: dict[str, Any]) -> str:
    """渲染提示词，替换 {{placeholder}}。"""

    def _replace(match: re.Match[str]) -> str:
        path = match.group(1)
        field_path = path[len("request."):]

        if field_path == "body":
            value: Any = request_body
        else:
            value = _deep_get(request_body, field_path)

        if value is None:
            return ""  # 找不到的字段用空字符串替换
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    return _PLACEHOLDER_RE.sub(_replace, template)