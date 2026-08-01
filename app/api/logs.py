"""Application event log reader for the logs page."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from ..config import settings

router = APIRouter()

_TAIL_BYTES = 800 * 1024  # 只读文件尾部，避免大文件全量加载


def _log_path() -> Path:
    return Path(settings.data_dir) / "logs" / "autolist.log"


@router.get("/api/logs/events")
async def log_events(limit: int = 200, level: str = "", query: str = "") -> list[dict[str, Any]]:
    """Return the most recent structured event lines, newest first.

    level: exact match on INFO/WARNING/ERROR; query: substring match on the
    whole line (event name, movie, site, error text ...).
    """
    safe_limit = max(1, min(limit, 500))
    path = _log_path()
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            start = max(0, size - _TAIL_BYTES)
            if start:
                handle.seek(start)
                handle.readline()  # 丢弃从中间开始的半行
            lines = handle.readlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if level and str(event.get("level", "")).upper() != level.upper():
            continue
        if query and query.lower() not in line.lower():
            continue
        events.append(event)
    return list(reversed(events[-safe_limit:]))
