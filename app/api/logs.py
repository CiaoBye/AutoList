"""Application event log reader for the logs page."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..responses import LogEvent

router = APIRouter()

_TAIL_BYTES = 800 * 1024  # 只读文件尾部，避免大文件全量加载


def _log_path() -> Path:
    return Path(settings.data_dir) / "logs" / "autolist.log"


def _timestamp(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


@router.get("/api/logs/events", response_model=list[LogEvent])
async def log_events(limit: int = 200, level: str = "", query: str = "", before: str = "") -> list[dict[str, Any]]:
    """Return the most recent structured event lines, newest first.

    level: INFO/WARNING/ERROR, several separated by commas; query: substring match on the
    whole line (event name, movie, site, error text ...); before: only events older than this
    timestamp (the oldest ``ts`` of the previous page), for loading earlier pages.
    """
    safe_limit = max(1, min(limit, 500))
    levels = {item.strip().upper() for item in level.split(",") if item.strip()}
    cursor = _timestamp(before) if before else None
    if before and cursor is None:
        raise HTTPException(422, "before 必须是日志时间")
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
        if levels and str(event.get("level", "")).upper() not in levels:
            continue
        if query and query.lower() not in line.lower():
            continue
        if cursor is not None:
            stamp = _timestamp(event.get("ts"))
            if stamp is None or stamp >= cursor:
                continue
        events.append(event)
    page = events[-safe_limit:]
    # 与本页最早一条同一时刻的事件一并返回，下一页按时间取更早的，避免分页边界漏掉或重复。
    first = len(events) - len(page)
    while first > 0 and page and events[first - 1].get("ts") == page[0].get("ts"):
        first -= 1
        page.insert(0, events[first])
    return list(reversed(page))


@router.delete("/api/logs/events")
async def clear_log_events() -> dict[str, Any]:
    """Truncate the event log file (rotated backups are left untouched)."""
    path = _log_path()
    try:
        if path.exists():
            path.write_text("", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(500, "日志文件不可写") from exc
    return {"deleted": True}
