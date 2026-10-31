"""
时间工具 — 统一的本地时间字符串与 Unix 毫秒时间戳转换。

约定：
  - 数据库 *_at 列存人可直接读的本地时间字符串：YYYY-MM-DD HH:MM:ss.SSS
  - 数据库 *_ts 列存 Unix 毫秒时间戳（整数），用于范围检索
"""

from __future__ import annotations

from datetime import datetime


def fmt_local_ts(d: datetime | int | float) -> str:
    """把 datetime / 毫秒时间戳 格式化为本地可读字符串 YYYY-MM-DD HH:MM:ss.SSS。"""
    if isinstance(d, (int, float)):
        d = datetime.fromtimestamp(d / 1000)
    return d.strftime("%Y-%m-%d %H:%M:%S.") + f"{d.microsecond // 1000:03d}"


def now_readable() -> str:
    """当前时间：本地可读字符串。"""
    return fmt_local_ts(datetime.now())


def now_ts() -> int:
    """当前时间：Unix 毫秒时间戳。"""
    return int(datetime.now().timestamp() * 1000)
