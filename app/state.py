"""Process-local runtime state (sensitive download contexts, task maps, caches)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import HTTPException

from .config import settings
from .security import token_matches

# 下载 URL、Cookie 等短命敏感字段只保存在进程内，容器重启后会自然失效。
raw_candidates: dict[str, dict[str, Any]] = {}
running_tasks: dict[int, asyncio.Task[None]] = {}
running_recognition_tasks: dict[int, asyncio.Task[None]] = {}
running_library_tasks: dict[int, asyncio.Task[None]] = {}
running_automation_tasks: dict[int, asyncio.Task[None]] = {}
scheduler_task: asyncio.Task[None] | None = None
download_cart_lock = asyncio.Lock()
site_icon_cache: dict[int, tuple[bytes, str]] = {}
poster_cache: dict[str, tuple[bytes, str]] = {}

AUTH_EXEMPT_PATHS = {"/", "/favicon.ico", "/api/health", "/cookiecloud", "/cookiecloud/"}
MAX_RUNNING_SEARCH_TASKS = 3
COOKIECLOUD_RATE_LIMIT = 10
COOKIECLOUD_RATE_WINDOW_SECONDS = 60
_cookiecloud_upload_times: list[float] = []


def enforce_cookiecloud_rate_limit() -> None:
    """Bound anonymous CookieCloud uploads to reduce disk-fill abuse."""
    now = time.time()
    cutoff = now - COOKIECLOUD_RATE_WINDOW_SECONDS
    while _cookiecloud_upload_times and _cookiecloud_upload_times[0] < cutoff:
        _cookiecloud_upload_times.pop(0)
    if len(_cookiecloud_upload_times) >= COOKIECLOUD_RATE_LIMIT:
        raise HTTPException(429, "CookieCloud 上传过于频繁，请稍后再试")
    _cookiecloud_upload_times.append(now)


def require_configured_cookiecloud_uuid(uuid_value: str) -> None:
    """Only the KEY configured in settings may read or write CookieCloud blobs."""
    configured = (settings.cookiecloud_key or "").strip()
    if not configured:
        raise HTTPException(503, "请先在设置中配置 CookieCloud 用户 KEY")
    if not token_matches(uuid_value, configured):
        raise HTTPException(403, "CookieCloud 用户 KEY 与服务端配置不匹配")


def enforce_search_task_capacity(active_count: int | None = None) -> None:
    active = active_count
    if active is None:
        active = sum(1 for task in running_tasks.values() if task and not task.done())
    if active >= MAX_RUNNING_SEARCH_TASKS:
        raise HTTPException(429, f"已有 {active} 个搜索任务在运行，请等待完成后再试")
