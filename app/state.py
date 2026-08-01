"""Process-local runtime state (sensitive download contexts, task maps, caches)."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

from fastapi import HTTPException

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
MAX_POSTER_ITEMS = 64
MAX_POSTER_BYTES = 32 * 1024 * 1024
RAW_CANDIDATE_TTL_SECONDS = 2 * 60 * 60
MAX_RAW_CANDIDATES = 5000
COOKIECLOUD_RATE_LIMIT = 10
COOKIECLOUD_RATE_WINDOW_SECONDS = 60
COOKIECLOUD_GET_RATE_LIMIT = 30
COOKIECLOUD_GET_RATE_WINDOW_SECONDS = 60
_cookiecloud_upload_times: list[float] = []
_cookiecloud_get_times: list[float] = []


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


def enforce_cookiecloud_rate_limit() -> None:
    """Bound anonymous CookieCloud uploads to reduce disk-fill abuse."""
    now = time.time()
    cutoff = now - COOKIECLOUD_RATE_WINDOW_SECONDS
    while _cookiecloud_upload_times and _cookiecloud_upload_times[0] < cutoff:
        _cookiecloud_upload_times.pop(0)
    if len(_cookiecloud_upload_times) >= COOKIECLOUD_RATE_LIMIT:
        raise HTTPException(429, "CookieCloud 上传过于频繁，请稍后再试")
    _cookiecloud_upload_times.append(now)


def enforce_cookiecloud_get_rate_limit() -> None:
    """Bound anonymous CookieCloud blob reads to slow down KEY enumeration.
    Uses a separate budget so Chrome extension uploads are not starved by reads.
    """
    now = time.time()
    cutoff = now - COOKIECLOUD_GET_RATE_WINDOW_SECONDS
    while _cookiecloud_get_times and _cookiecloud_get_times[0] < cutoff:
        _cookiecloud_get_times.pop(0)
    if len(_cookiecloud_get_times) >= COOKIECLOUD_GET_RATE_LIMIT:
        raise HTTPException(429, "CookieCloud 读取过于频繁，请稍后再试")
    _cookiecloud_get_times.append(now)


def require_configured_cookiecloud_uuid(uuid_value: str) -> None:
    """Only the KEY configured in settings may read or write CookieCloud blobs."""
    configured = (settings.cookiecloud_key or "").strip()
    if not configured:
        raise HTTPException(503, "请先在设置中配置 CookieCloud 用户 KEY")
    if not token_matches(uuid_value, configured):
        raise HTTPException(403, "CookieCloud 用户 KEY 与服务端配置不匹配")


def remember_site_icon(site_id: int, value: tuple[bytes, str]) -> None:
    """Cache a proxied site icon with an LRU-style entry cap."""
    site_icon_cache[site_id] = value
    site_icon_cache.move_to_end(site_id)
    while len(site_icon_cache) > MAX_SITE_ICONS:
        site_icon_cache.popitem(last=False)


def remember_poster(cache_key: str, value: tuple[bytes, str]) -> None:
    """Cache a poster with both entry-count and total-byte caps."""
    poster_cache[cache_key] = value
    poster_cache.move_to_end(cache_key)
    total = sum(len(content) for content, _ in poster_cache.values())
    while (len(poster_cache) > MAX_POSTER_ITEMS or total > MAX_POSTER_BYTES) and poster_cache:
        _, (content, _) = poster_cache.popitem(last=False)
        total -= len(content)


def enforce_background_task_capacity(registry: dict[int, asyncio.Task[None]], limit: int, label: str) -> None:
    """Bound process-wide background task concurrency (recognition/library/automation)."""
    active = sum(1 for task in registry.values() if task and not task.done())
    if active >= limit:
        raise HTTPException(429, f"已有 {active} 个{label}任务在运行，请稍后再试")


def enforce_search_task_capacity(active_count: int | None = None) -> None:
    active = active_count
    if active is None:
        active = sum(1 for task in running_tasks.values() if task and not task.done())
    if active >= MAX_RUNNING_SEARCH_TASKS:
        raise HTTPException(429, f"已有 {active} 个搜索任务在运行，请等待完成后再试")
