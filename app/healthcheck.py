"""Container healthcheck: local process, scheduler heartbeat and SQLite file.

Deliberately does not call TMDB, PT sites, Emby, Transmission, or MoviePilot.
The scheduler check distinguishes soft degradation from a stopped or stale
heartbeat, so an optional playlist sync failure does not by itself flip the
container unhealthy while a dead scheduler still fails health checks.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from typing import Any

from .state import SCHEDULER_HEARTBEAT_TIMEOUT_SECONDS


def validate_scheduler_health(payload: dict[str, Any]) -> None:
    """Reject a stopped or genuinely stale scheduler, but allow soft degradation."""
    scheduler = payload.get("scheduler")
    if not isinstance(scheduler, dict):
        raise RuntimeError("调度器健康状态缺失")
    status = str(scheduler.get("status") or "").strip().lower()
    if status in {"stopped", "stale"}:
        raise RuntimeError(f"调度器状态异常：{status}")
    # Older state snapshots can report ``degraded`` while the heartbeat is
    # already stale.  Check the age independently so a persistent tick error
    # cannot mask a dead task.
    try:
        heartbeat_age = float(scheduler.get("last_heartbeat_age_seconds"))
    except (TypeError, ValueError):
        heartbeat_age = None
    if heartbeat_age is not None and heartbeat_age > SCHEDULER_HEARTBEAT_TIMEOUT_SECONDS:
        raise RuntimeError("调度器心跳已长期失效")
    if status not in {"starting", "degraded", "ok"}:
        raise RuntimeError(f"调度器状态未知：{status or 'missing'}")


def check() -> None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=2) as response:
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"健康检查接口不可用：{exc}") from exc
    if response.status != 200 or not payload.get("ok"):
        raise RuntimeError("API 健康检查失败")
    validate_scheduler_health(payload)
    try:
        database = sqlite3.connect("/data/playlist-autodown.db", timeout=2)
        try:
            database.execute("PRAGMA query_only=ON")
            quick_check = database.execute("PRAGMA quick_check(1)").fetchone()
            has_playlists = database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='playlists'"
            ).fetchone()
        finally:
            database.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite 健康检查失败：{exc}") from exc
    if not quick_check or quick_check[0] != "ok":
        raise RuntimeError("SQLite quick_check 失败")
    if not has_playlists:
        raise RuntimeError("playlists 表缺失")


if __name__ == "__main__":
    check()
