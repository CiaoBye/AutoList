"""海报预热：在后台把片单影片的海报下载到缓存，页面第一次打开时不必等上游（fanart.tv / TMDB）。"""

from __future__ import annotations

import asyncio
import time

from ..database import connect
from ..logs import event_logger
from ..security import safe_error

WARM_INTERVAL_SECONDS = 24 * 3600
# 进程启动后先等一会儿再预热，不和启动时的同步、识别抢资源。
WARM_START_DELAY_SECONDS = 60
WARM_CONCURRENCY = 2

_started = time.monotonic()
_last_warm: float | None = None
_background: set[asyncio.Task[None]] = set()


def warm_due() -> bool:
    now = time.monotonic()
    if now - _started < WARM_START_DELAY_SECONDS:
        return False
    return _last_warm is None or now - _last_warm >= WARM_INTERVAL_SECONDS


async def warm_posters() -> int:
    """依次请求每部已识别影片的海报接口（已缓存的立即返回），返回处理的影片数。"""
    global _last_warm
    _last_warm = time.monotonic()
    from ..api.images import playlist_item_fanart_poster

    with connect() as conn:
        item_ids = [int(row[0]) for row in conn.execute(
            """SELECT id FROM playlist_items WHERE tmdb_id IS NOT NULL
               ORDER BY (library_state='in_library') DESC, playlist_id, rank_no""",
        ).fetchall()]
    slots = asyncio.Semaphore(WARM_CONCURRENCY)

    async def one(item_id: int) -> None:
        async with slots:
            try:
                await playlist_item_fanart_poster(item_id, "")
            except Exception as exc:  # 单张失败不影响其余；页面请求时会再试
                event_logger().info("poster_warm_skipped", extra={"detail": f"#{item_id} {safe_error(exc)}"})

    await asyncio.gather(*(one(item_id) for item_id in item_ids))
    return len(item_ids)


def start_warm() -> None:
    """在后台启动一次预热（已有一次在跑时不重复）。"""
    if any(not task.done() for task in _background):
        return

    async def run() -> None:
        try:
            count = await warm_posters()
            event_logger().info("poster_warmed", extra={"detail": f"海报预热完成：{count} 部影片", "total": count})
        except Exception as exc:
            event_logger().warning("poster_warm_failed", extra={"error": safe_error(exc)})

    task = asyncio.get_running_loop().create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)
