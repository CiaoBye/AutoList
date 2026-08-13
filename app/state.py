"""Process-local runtime state (sensitive download contexts, task maps, caches)."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any


from .config import settings
from .security import token_matches

# 下载 URL、Cookie 等短命敏感字段只保存在进程内，容器重启后会自然失效。
raw_candidates: dict[str, dict[str, Any]] = {}
raw_candidate_seen_at: dict[str, float] = {}
running_tasks: dict[int, asyncio.Task[None]] = {}
running_recognition_tasks: dict[int, asyncio.Task[None]] = {}
running_library_tasks: dict[int, asyncio.Task[None]] = {}
running_automation_tasks: dict[int, asyncio.Task[None]] = {}
scheduler_task: asyncio.Task[None] | None = None
download_cart_lock = asyncio.Lock()
site_icon_cache: OrderedDict[int, tuple[bytes, str]] = OrderedDict()
poster_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

AUTH_EXEMPT_PATHS = {"/", "/favicon.ico", "/api/health", "/cookiecloud", "/cookiecloud/"}
MAX_RUNNING_SEARCH_TASKS = 3
MAX_RUNNING_RECOGNITION_TASKS = 2
MAX_RUNNING_LIBRARY_TASKS = 2
MAX_RUNNING_AUTOMATION_TASKS = 2
MAX_SITE_ICONS = 128
MAX_SITE_ICON_BYTES = 8 * 1024 * 1024
MAX_POSTER_ITEMS = 64
MAX_POSTER_BYTES = 32 * 1024 * 1024
RAW_CANDIDATE_TTL_SECONDS = 2 * 60 * 60
MAX_RAW_CANDIDATES = 5000
COOKIECLOUD_RATE_LIMIT = 10
COOKIECLOUD_RATE_WINDOW_SECONDS = 60
COOKIECLOUD_GET_RATE_LIMIT = 30
COOKIECLOUD_GET_RATE_WINDOW_SECONDS = 60
SCHEDULER_HEARTBEAT_TIMEOUT_SECONDS = 180
_cookiecloud_upload_times: list[float] = []
_cookiecloud_get_times: list[float] = []

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
    raw_candidate_seen_at[candidate_id] = time.time()


def forget_raw_candidate(candidate_id: str) -> None:
    raw_candidates.pop(candidate_id, None)
    raw_candidate_seen_at.pop(candidate_id, None)


def prune_raw_candidates(now: float | None = None) -> None:
    """Expire sensitive download contexts and keep memory bounded."""
    current = time.time() if now is None else now
    for candidate_id in list(raw_candidates):
        seen_at = raw_candidate_seen_at.setdefault(candidate_id, current)
        if current - seen_at > RAW_CANDIDATE_TTL_SECONDS:
            forget_raw_candidate(candidate_id)
    if len(raw_candidates) <= MAX_RAW_CANDIDATES:
        return
    ordered = sorted(raw_candidates, key=lambda candidate_id: raw_candidate_seen_at.get(candidate_id, current))
    for candidate_id in ordered[:len(raw_candidates) - MAX_RAW_CANDIDATES]:
        forget_raw_candidate(candidate_id)


# 匿名上传宽松桶（审计 2-4 残余）：未认证请求在 body 读取前受限，
# 防止无限并发大体积请求压内存；合法请求仍由 enforce_cookiecloud_rate_limit 严格限流。
COOKIECLOUD_ANONYMOUS_RATE_LIMIT = 120
_cookiecloud_anonymous_times: list[float] = []


def enforce_cookiecloud_anonymous_rate_limit() -> str | None:
    now = time.time()
    cutoff = now - COOKIECLOUD_RATE_WINDOW_SECONDS
    while _cookiecloud_anonymous_times and _cookiecloud_anonymous_times[0] < cutoff:
        _cookiecloud_anonymous_times.pop(0)
    if len(_cookiecloud_anonymous_times) >= COOKIECLOUD_ANONYMOUS_RATE_LIMIT:
        return "CookieCloud 请求过于频繁，请稍后再试"
    _cookiecloud_anonymous_times.append(now)
    return None


def enforce_cookiecloud_rate_limit() -> str | None:
    """Bound anonymous CookieCloud uploads to reduce disk-fill abuse.
    返回错误消息而非抛框架异常（审计 3-4）：由路由层转换为 HTTP 响应。
    """
    now = time.time()
    cutoff = now - COOKIECLOUD_RATE_WINDOW_SECONDS
    while _cookiecloud_upload_times and _cookiecloud_upload_times[0] < cutoff:
        _cookiecloud_upload_times.pop(0)
    if len(_cookiecloud_upload_times) >= COOKIECLOUD_RATE_LIMIT:
        return "CookieCloud 上传过于频繁，请稍后再试"
    _cookiecloud_upload_times.append(now)
    return None


def enforce_cookiecloud_get_rate_limit() -> str | None:
    """Bound anonymous CookieCloud blob reads to slow down KEY enumeration.
    Uses a separate budget so Chrome extension uploads are not starved by reads.
    """
    now = time.time()
    cutoff = now - COOKIECLOUD_GET_RATE_WINDOW_SECONDS
    while _cookiecloud_get_times and _cookiecloud_get_times[0] < cutoff:
        _cookiecloud_get_times.pop(0)
    if len(_cookiecloud_get_times) >= COOKIECLOUD_GET_RATE_LIMIT:
        return "CookieCloud 读取过于频繁，请稍后再试"
    _cookiecloud_get_times.append(now)
    return None


def require_configured_cookiecloud_uuid(uuid_value: str) -> tuple[int, str] | None:
    """Only the KEY configured in settings may read or write CookieCloud blobs.
    返回 (status_code, detail) 或 None（审计 3-4）：由路由层转换为 HTTP 响应。
    """
    configured = (settings.cookiecloud_key or "").strip()
    if not configured:
        return 503, "请先在设置中配置 CookieCloud 用户 KEY"
    if not token_matches(uuid_value, configured):
        return 403, "CookieCloud 用户 KEY 与服务端配置不匹配"
    return None


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


def enforce_background_task_capacity(registry: dict[int, asyncio.Task[None]], limit: int, label: str) -> str | None:
    """Bound process-wide background task concurrency (recognition/library/automation)."""
    active = sum(1 for task in registry.values() if task and not task.done())
    if active >= limit:
        return f"已有 {active} 个{label}任务在运行，请稍后再试"
    return None


def enforce_search_task_capacity(active_count: int | None = None) -> str | None:
    active = active_count
    if active is None:
        active = sum(1 for task in running_tasks.values() if task and not task.done())
    if active >= MAX_RUNNING_SEARCH_TASKS:
        return f"已有 {active} 个搜索任务在运行，请等待完成后再试"
    return None
