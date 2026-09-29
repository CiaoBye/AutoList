"""Process-local runtime state (download contexts, locks, caches, rate limits); background tasks live in app/tasks.py."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any


from .contexts import CandidateContexts

# 下载 URL、Cookie 等短命敏感字段只保存在进程内，容器重启后会自然失效。
# 候选下载上下文：加密存数据库（见 app/contexts.py），服务重启不丢失。
raw_candidates = CandidateContexts()
scheduler_task: asyncio.Task[None] | None = None
selection_submit_lock = asyncio.Lock()
site_icon_cache: OrderedDict[int, tuple[bytes, str]] = OrderedDict()
poster_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

AUTH_EXEMPT_PATHS = {"/", "/favicon.ico", "/api/health"}
MAX_SITE_ICONS = 128
MAX_SITE_ICON_BYTES = 8 * 1024 * 1024
MAX_POSTER_ITEMS = 300
MAX_POSTER_BYTES = 32 * 1024 * 1024
SCHEDULER_HEARTBEAT_TIMEOUT_SECONDS = 180

# The scheduler is deliberately kept process-local, like the existing task
# registries.  These fields are a diagnostic heartbeat, not a distributed
# lock.  Persisting the actual task state remains a separate concern.
scheduler_running = False
scheduler_started_at: str | None = None
scheduler_last_heartbeat_at: str | None = None
scheduler_last_success_at: str | None = None
scheduler_last_error: str | None = None
_scheduler_last_heartbeat_monotonic: float | None = None


def mark_scheduler_started(timestamp: str) -> None:
    global scheduler_running, scheduler_started_at, scheduler_last_error
    scheduler_running = True
    scheduler_started_at = timestamp
    scheduler_last_error = None


def mark_scheduler_heartbeat(timestamp: str, monotonic_now: float | None = None) -> None:
    global scheduler_running, scheduler_last_heartbeat_at, _scheduler_last_heartbeat_monotonic
    scheduler_running = True
    scheduler_last_heartbeat_at = timestamp
    _scheduler_last_heartbeat_monotonic = time.monotonic() if monotonic_now is None else monotonic_now


def mark_scheduler_success(timestamp: str) -> None:
    global scheduler_last_success_at, scheduler_last_error
    scheduler_last_success_at = timestamp
    scheduler_last_error = None


def mark_scheduler_error(message: str) -> None:
    global scheduler_last_error
    # The value is exposed by /api/health, so sanitize it at the state boundary.
    from .security import sanitize_sensitive_text

    scheduler_last_error = sanitize_sensitive_text(message, 500)


def mark_scheduler_stopped() -> None:
    global scheduler_running
    scheduler_running = False


def scheduler_health(monotonic_now: float | None = None) -> dict[str, Any]:
    """Return a backwards-compatible diagnostic snapshot for the scheduler."""
    now = time.monotonic() if monotonic_now is None else monotonic_now
    age = None
    if _scheduler_last_heartbeat_monotonic is not None:
        age = max(0.0, now - _scheduler_last_heartbeat_monotonic)
    if not scheduler_running:
        status, ok = "stopped", False
    elif age is None:
        # The task has started but has not reached its first tick yet.
        status, ok = "starting", True
    elif scheduler_last_error:
        status, ok = "degraded", False
    elif age > SCHEDULER_HEARTBEAT_TIMEOUT_SECONDS:
        status, ok = "stale", False
    else:
        status, ok = "ok", True
    return {
        "ok": ok,
        "status": status,
        "running": scheduler_running,
        "started_at": scheduler_started_at,
        "last_heartbeat_at": scheduler_last_heartbeat_at,
        "last_success_at": scheduler_last_success_at,
        "last_error": scheduler_last_error,
        "last_heartbeat_age_seconds": round(age, 3) if age is not None else None,
    }


def remember_raw_candidate(candidate_id: str, value: dict[str, Any]) -> None:
    raw_candidates[candidate_id] = value


def forget_raw_candidate(candidate_id: str) -> None:
    raw_candidates.pop(candidate_id, None)


def prune_raw_candidates() -> None:
    """Expire encrypted download contexts and drop ones whose candidate was cleaned up."""
    raw_candidates.prune()


def remember_site_icon(site_id: int, value: tuple[bytes, str]) -> None:
    """Cache a proxied site icon with LRU entry-count and total-byte caps (审计 3-26)."""
    site_icon_cache[site_id] = value
    site_icon_cache.move_to_end(site_id)
    total = sum(len(content) for content, _ in site_icon_cache.values())
    while (len(site_icon_cache) > MAX_SITE_ICONS or total > MAX_SITE_ICON_BYTES) and site_icon_cache:
        _, (content, _) = site_icon_cache.popitem(last=False)
        total -= len(content)


def remember_poster(cache_key: str, value: tuple[bytes, str]) -> None:
    """Cache a poster with both entry-count and total-byte caps."""
    poster_cache[cache_key] = value
    poster_cache.move_to_end(cache_key)
    total = sum(len(content) for content, _ in poster_cache.values())
    while (len(poster_cache) > MAX_POSTER_ITEMS or total > MAX_POSTER_BYTES) and poster_cache:
        _, (content, _) = poster_cache.popitem(last=False)
        total -= len(content)


# 最近一次 CookieCloud 同步的结果（进程内保存；服务启动后的第一轮定时拉取就会重新产生）。
last_cookie_sync: dict[str, Any] = {}
