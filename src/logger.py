"""
日志 — 使用 Python logging 模拟 pino 的行为。

双输出: 文件 + 控制台（彩色）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


class _ColoredFormatter(logging.Formatter):
    """简单的 ANSI 彩色格式化器（模拟 pino-pretty）。"""

    COLORS = {
        "DEBUG": "\033[36m",    # cyan
        "INFO": "\033[32m",     # green
        "WARNING": "\033[33m",  # yellow
        "ERROR": "\033[31m",    # red
        "CRITICAL": "\033[35m",  # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        level = f"{color}[{record.levelname}]{self.RESET}"
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        return f"{ts} {level} {record.getMessage()} ({record.name})"


_logger: logging.Logger | None = None


def init_logger(data_dir: str, debug: bool = False) -> logging.Logger:
    """初始化日志系统。"""
    global _logger

    level = logging.DEBUG if debug else logging.INFO
    _logger = logging.getLogger("doc_agent_py")
    _logger.setLevel(level)
    _logger.handlers.clear()

    # 文件 handler
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(str(log_dir / "app.log"), encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s (%(name)s)",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(fh)

    # 控制台 handler（彩色）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(_ColoredFormatter())
    _logger.addHandler(ch)

    return _logger


def get_logger() -> logging.Logger:
    """获取日志器（未初始化时返回 fallback）。"""
    global _logger
    if _logger is None:
        _logger = logging.getLogger("doc_agent_py")
        _logger.setLevel(logging.INFO)
        if not _logger.handlers:
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(_ColoredFormatter())
            _logger.addHandler(ch)
    return _logger