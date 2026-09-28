"""Application event logging: JSON-lines file with rotation.

日志写入 data/logs/autolist.log（10MB × 5 轮转），每行一条结构化 JSON，
供“日志”页面与后续排查使用；容器 stdout 不受影响。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .security import sanitize_sensitive_text

_configured = False

# 允许通过 logger.info(..., extra={...}) 携带的结构化字段白名单。
_EVENT_FIELDS = (
    "task_id", "rank", "movie", "site", "candidate_id", "status",
    "total", "completed", "matched", "results", "kept", "excluded",
    "reason", "detail", "count", "error", "duration_ms", "hash", "submitted", "skipped",
    "stage", "trigger",
)


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": sanitize_sensitive_text(record.getMessage(), 1000),
        }
        for key in _EVENT_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                if isinstance(value, str):
                    payload[key] = sanitize_sensitive_text(value, 1000)
                else:
                    payload[key] = value
        if record.exc_info:
            payload["error"] = sanitize_sensitive_text(self.formatException(record.exc_info), 2000)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """幂等初始化文件日志；data_dir 变更（测试）后重新指向新目录。"""
    global _configured
    logs_dir = Path(settings.data_dir) / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            logs_dir / "autolist.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    except OSError:
        # 数据目录不可写时静默降级：应用日志缺文件不影响功能。
        return
    handler.setFormatter(JsonLineFormatter())
    logger = logging.getLogger("autolist")
    logger.setLevel(logging.INFO)
    for old_handler in logger.handlers[:]:
        logger.removeHandler(old_handler)
        old_handler.close()
    logger.addHandler(handler)
    logger.propagate = False
    _configured = True


def event_logger() -> logging.Logger:
    if not _configured:
        configure_logging()
    return logging.getLogger("autolist")
