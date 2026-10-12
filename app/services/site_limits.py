"""站点访问频率：同一站点相邻请求的最小间隔、单位周期内的请求次数上限（所有搜索与检测共用）。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from ..util import to_float, to_int


# 站点访问频率（与 MoviePilot 站点字段语义一致）：单位周期 limit_interval 秒内最多 limit_count 次请求，
# 访问间隔 limit_gap 秒是相邻两次请求之间至少隔多久（没设置用 SITE_REQUEST_GAP_SECONDS）；
# 只配置 limit_interval 时视为两次请求之间的最小间隔。每个站点一把锁，保证并发任务也按序等待。
_site_request_times: dict[int, deque[float]] = {}

_site_rate_locks: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}

# 同一站点相邻两次搜索请求至少间隔的秒数（站点自己配置了更严格的频率时以站点为准）。
SITE_REQUEST_GAP_SECONDS = 3.0

_site_last_request: dict[int, float] = {}

async def wait_for_site_rate_limit(site: dict[str, Any]) -> None:
    site_id = to_int(site.get("id") or 0)
    if not site_id:
        return
    gap = to_float(site.get("limit_gap")) if site.get("limit_gap") is not None else SITE_REQUEST_GAP_SECONDS
    await _wait_for_site_gap(site_id, max(0.0, gap))
    interval = to_float(site.get("limit_interval")) if site.get("limit_interval") is not None else 0.0
    if interval <= 0:
        return
    count = max(1, to_int(site.get("limit_count") or 1))
    loop = asyncio.get_running_loop()
    entry = _site_rate_locks.get(site_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _site_rate_locks[site_id] = entry
    async with entry[1]:
        history = _site_request_times.setdefault(site_id, deque())
        now = time.monotonic()
        while history and now - history[0] >= interval:
            history.popleft()
        if len(history) >= count:
            await asyncio.sleep(max(0.0, interval - (now - history[0])))
            now = time.monotonic()
            while history and now - history[0] >= interval:
                history.popleft()
        history.append(time.monotonic())

async def _wait_for_site_gap(site_id: int, gap: float = SITE_REQUEST_GAP_SECONDS) -> None:
    """同一站点的请求排队，相邻两次之间至少隔 gap 秒（默认 SITE_REQUEST_GAP_SECONDS）。"""
    loop = asyncio.get_running_loop()
    entry = _site_gap_locks.get(site_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _site_gap_locks[site_id] = entry
    async with entry[1]:
        last = _site_last_request.get(site_id)
        if last is not None:
            remaining = gap - (time.monotonic() - last)
            if remaining > 0:
                await asyncio.sleep(remaining)
        _site_last_request[site_id] = time.monotonic()

_site_gap_locks: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
