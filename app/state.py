"""Process-local runtime state (download contexts, locks, caches, rate limits); background tasks live in app/tasks.py."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import settings
from .contexts import CandidateContexts

# 下载 URL、Cookie 等短命敏感字段只保存在进程内，容器重启后会自然失效。
# 候选下载上下文：加密存数据库（见 app/contexts.py），服务重启不丢失。
raw_candidates = CandidateContexts()
scheduler_task: asyncio.Task[None] | None = None
selection_submit_lock = asyncio.Lock()
site_icon_cache: OrderedDict[int, tuple[bytes, str]] = OrderedDict()


class PosterCache(OrderedDict):  # type: ignore[type-arg]
    """海报与剧照缓存：进程内 LRU，加上数据目录里的磁盘副本。

    海报来自 fanart.tv / TMDB / Emby，首次下载要 2 到 4 秒；磁盘副本让重启、部署之后不必重新下载。
    读取顺序是内存、磁盘；写入同时写内存与磁盘（磁盘上限 ``MAX_DISK_FILES`` 个，超过后删最旧的）。
    """

    DISK_PRUNE_EVERY = 50

    def __init__(self) -> None:
        super().__init__()
        self._writes = 0

    @staticmethod
    def _path(key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return Path(settings.data_dir) / "cache" / "images" / digest[:2] / digest

    def _disk_read(self, key: str) -> tuple[bytes, str] | None:
        try:
            raw = self._path(key).read_bytes()
        except OSError:
            return None
        head, _, content = raw.partition(b"\n")
        return (content, head.decode("ascii", "ignore")) if content and head else None

    def disk_write(self, key: str, value: tuple[bytes, str]) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(value[1].encode("ascii", "ignore") + b"\n" + value[0])
            os.replace(temporary, path)
        except OSError:
            return  # 磁盘缓存只是加速，写不进去不影响功能
        self._writes += 1
        if self._writes % self.DISK_PRUNE_EVERY == 0:
            self._prune_disk()

    def _prune_disk(self) -> None:
        root = Path(settings.data_dir) / "cache" / "images"
        try:
            files = sorted((item for item in root.glob("*/*") if item.is_file() and item.suffix != ".tmp"), key=lambda item: item.stat().st_mtime)
        except OSError:
            return
        for item in files[:max(0, len(files) - MAX_DISK_FILES)]:
            try:
                item.unlink()
            except OSError:
                pass

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        value = super().get(key)
        if value is None:
            value = self._disk_read(key)
            if value is not None:
                self._store(key, value)
        return default if value is None else value

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key) or (isinstance(key, str) and self._path(key).exists())

    def __getitem__(self, key: str) -> tuple[bytes, str]:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value  # type: ignore[no-any-return]

    def _store(self, key: str, value: tuple[bytes, str]) -> None:
        OrderedDict.__setitem__(self, key, value)
        self.move_to_end(key)
        total = sum(len(content) for content, _ in self.values())
        while (len(self) > MAX_POSTER_ITEMS or total > MAX_POSTER_BYTES) and len(self):
            _, (content, _) = self.popitem(last=False)
            total -= len(content)


poster_cache = PosterCache()
_fetches: dict[str, asyncio.Future[Any]] = {}


async def fetch_once(key: str, fetch: Callable[[], Awaitable[Any]]) -> Any:
    """同一个 key 的下载同时只做一次：并发的请求（浏览器一次要几十张海报）共用结果。"""
    pending = _fetches.get(key)
    if pending is not None and pending.get_loop() is asyncio.get_running_loop():
        return await asyncio.shield(pending)
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    _fetches[key] = future
    try:
        result = await fetch()
    except BaseException as exc:
        future.set_exception(exc)
        future.exception()  # 没有别的等待者时避免 “exception was never retrieved”
        raise
    else:
        future.set_result(result)
        return result
    finally:
        if _fetches.get(key) is future:
            del _fetches[key]

AUTH_EXEMPT_PATHS = {"/", "/favicon.ico", "/api/health"}
MAX_SITE_ICONS = 128
MAX_SITE_ICON_BYTES = 8 * 1024 * 1024
MAX_POSTER_ITEMS = 800
MAX_POSTER_BYTES = 64 * 1024 * 1024
# 磁盘上的海报副本最多保留的文件数（每张约 40 KB，两份片单加剧照约 100 MB 以内）。
MAX_DISK_FILES = 4000
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
    poster_cache._store(cache_key, value)
    poster_cache.disk_write(cache_key, value)


# 最近一次 CookieCloud 同步的结果（进程内保存；服务启动后的第一轮定时拉取就会重新产生）。
last_cookie_sync: dict[str, Any] = {}
