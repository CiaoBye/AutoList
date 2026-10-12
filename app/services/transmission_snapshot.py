"""Transmission 下载列表的短时缓存与“实时读取”（页面展示用缓存、提交与自动删除只认实时数据）。"""

from __future__ import annotations

import asyncio
from typing import Any

from ..clients import TransmissionClient


# Transmission 下载列表短 TTL 缓存（审计 3-25）：避免片单每次刷新重复 RPC。
_transmission_snapshot_cache: dict[int, tuple[float, list[dict[str, Any]], str]] = {}

TRANSMISSION_SNAPSHOT_TTL_SECONDS = 10

def invalidate_downloads_snapshot() -> None:
    _transmission_snapshot_cache.clear()

# 最近一次成功读到的下载列表。Transmission 正忙（大量下载、校验）时读取会超时，
# 短时间内沿用它，免得状态忽明忽暗、提交被无谓地挡住。
_last_good_downloads: tuple[float, list[dict[str, Any]]] | None = None

TRANSMISSION_READ_TIMEOUT_SECONDS = 15

TRANSMISSION_STALE_SECONDS = 300

def _recent_downloads(max_age: float) -> list[dict[str, Any]] | None:
    import time as _time

    if _last_good_downloads is not None and _time.monotonic() - _last_good_downloads[0] <= max_age:
        return _last_good_downloads[1]
    return None

async def _read_downloads(timeout: float) -> list[dict[str, Any]]:
    import time as _time

    global _last_good_downloads
    torrents = await asyncio.wait_for(TransmissionClient().current_downloads(), timeout=timeout)
    _last_good_downloads = (_time.monotonic(), torrents)
    return torrents

async def fetch_downloads_now() -> list[dict[str, Any]]:
    """实时读取 Transmission 的下载列表（失败直接抛出），并刷新页面展示用的缓存。

    自动删除与提交前的防重复只认这一份实时数据：旧快照不能证明某个任务“现在”是否存在。"""
    import time as _time

    torrents = await _read_downloads(TRANSMISSION_READ_TIMEOUT_SECONDS)
    _transmission_snapshot_cache[0] = (_time.monotonic(), torrents, "known_present" if torrents else "known_empty")
    return torrents

async def downloads_for_submit() -> list[dict[str, Any]]:
    """提交前确认 Transmission 里正在下载什么，用来跳过已在下载的相同发布。

    必须是实时读取的结果：先等 15 秒，失败再重试一次（30 秒）；仍失败就报错、暂缓提交。
    旧快照只用来展示，不能当作防重复的依据（新加入的下载不在里面）。"""
    last_error: Exception | None = None
    for timeout in (TRANSMISSION_READ_TIMEOUT_SECONDS, TRANSMISSION_READ_TIMEOUT_SECONDS * 2):
        try:
            return await _read_downloads(timeout)
        except Exception as exc:
            last_error = exc
    raise RuntimeError("无法读取 Transmission 的下载列表") from last_error

async def _current_downloads_cached(force: bool = False) -> tuple[list[dict[str, Any]], str]:
    import time as _time

    now = _time.monotonic()
    cached = _transmission_snapshot_cache.get(0)
    if not force and cached is not None and now - cached[0] < TRANSMISSION_SNAPSHOT_TTL_SECONDS:
        return cached[1], cached[2]
    try:
        torrents = await _read_downloads(TRANSMISSION_READ_TIMEOUT_SECONDS)
        state = "known_present" if torrents else "known_empty"
    except Exception:
        # 只用于页面展示：读取失败时沿用 5 分钟内的上一次结果，而不是立刻把所有影片的下载状态变成“未知”。
        # 自动删除与提交不走这里（见 fetch_downloads_now / downloads_for_submit）。
        recent = _recent_downloads(TRANSMISSION_STALE_SECONDS)
        torrents, state = (recent, "known_present" if recent else "known_empty") if recent is not None else ([], "unknown")
    _transmission_snapshot_cache[0] = (_time.monotonic(), torrents, state)
    return torrents, state
