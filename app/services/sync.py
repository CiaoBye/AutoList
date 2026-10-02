"""统一同步：把 Transmission、MoviePilot、Emby 三方的最新情况对齐到片单影片。

影片状态本身由 ``films.project_films`` 实时计算；这里负责让它读到的数据是新的：
刷新 Transmission 快照与 MoviePilot 整理结果的缓存，识别 Transmission 里不是经 AutoList 提交的种子
（手动添加的也对得上片单影片），并立即向 Emby 复查正在下载的影片。三处共用同一个入口，
定时任务、打开首页与“立即同步”按钮都走 ``reconcile``，同一时刻只会有一次在跑。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..clients import EmbyClient, MoviePilotClient, TransmissionClient
from ..config import settings
from ..database import connect
from ..logs import event_logger
from ..queries import films as film_queries
from ..security import safe_error, sanitize_sensitive_text
from ..util import utc_now
from .downloads import identify_torrents
from .films import project_films, replaced_stalled_torrents
from .history import organize_failures
from .library import library_recheck_due, recheck_library_states
from .search import _current_downloads_cached, invalidate_downloads_snapshot
from .sites import test_site_config
from ..tasks import SEARCH

# 定时同步的间隔；打开首页时若距上次同步不足这个间隔就不重复触发。
SYNC_INTERVAL_SECONDS = 120.0
KICK_MIN_GAP_SECONDS = 30.0
SOURCE_TIMEOUT_SECONDS = 15.0
IDENTIFY_TIMEOUT_SECONDS = 25.0
SOURCES = ("transmission", "moviepilot", "emby")
# 检测失败或搜不到的站点隔这么久自动重新检测一次，每次同步最多检测几个；恢复后首页不再提示。
SITE_RETEST_MINUTES = 10
SITE_RETEST_PER_RUN = 3

_inflight: asyncio.Task[dict[str, Any]] | None = None
_last_started: float | None = None
_last_result: dict[str, Any] | None = None
_background: set[asyncio.Task[Any]] = set()


def _source(configured: bool, ok: bool, message: str | None = None) -> dict[str, Any]:
    return {"configured": configured, "ok": ok, "checked_at": utc_now(), "message": message}


def _failed(name: str, exc: Exception) -> dict[str, Any]:
    return _source(True, False, sanitize_sensitive_text(f"无法读取 {name}：{safe_error(exc)}", 200))


async def _check_transmission() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not TransmissionClient().base_url:
        return _source(False, False, "未配置 Transmission"), []
    torrents, state = await _current_downloads_cached(force=True)
    if state == "unknown":
        return _source(True, False, "无法读取 Transmission 的下载列表"), []
    return _source(True, True), torrents


async def _check_moviepilot() -> dict[str, Any]:
    if not settings.mp_base_url or not settings.mp_api_key:
        return _source(False, False, "未配置 MoviePilot")
    try:
        await asyncio.wait_for(MoviePilotClient().transfer_history(1, 1), timeout=SOURCE_TIMEOUT_SECONDS)
    except Exception as exc:
        return _failed("MoviePilot", exc)
    return _source(True, True)


async def _check_emby() -> dict[str, Any]:
    if not settings.emby_base_url or not settings.emby_api_key:
        return _source(False, False, "未配置 Emby")
    try:
        await asyncio.wait_for(EmbyClient().check(), timeout=SOURCE_TIMEOUT_SECONDS)
    except Exception as exc:
        return _failed("Emby", exc)
    return _source(True, True)


def _known_hashes() -> set[str]:
    with connect() as conn:
        rows = conn.execute("SELECT submission_hash FROM download_history WHERE success=1 AND submission_hash IS NOT NULL").fetchall()
    return {str(row[0]).strip().casefold() for row in rows if str(row[0]).strip()}


async def retest_failing_sites() -> int:
    """重新检测参与搜索、上次检测失败或搜不到的站点，返回恢复正常的数量。寻片进行中不检测，免得多出请求。"""
    if SEARCH.active_count():
        return 0
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM pt_sites WHERE enabled=1 AND search_enabled=1 AND last_status IN ('error','empty')
                 AND (last_tested_at IS NULL OR datetime(last_tested_at) < datetime('now', ?))
               ORDER BY last_tested_at LIMIT ?""",
            (f"-{SITE_RETEST_MINUTES} minutes", SITE_RETEST_PER_RUN),
        ).fetchall()
    recovered = 0
    for row in rows:
        try:
            result = await asyncio.wait_for(test_site_config(dict(row)), timeout=45)
        except Exception as exc:
            event_logger().warning("sync_site_retest_failed", extra={"site": str(row["name"]), "error": safe_error(exc)})
            continue
        if result["ok"]:
            recovered += 1
            event_logger().info("sync_site_recovered", extra={"site": str(row["name"]), "detail": f"站点【{row['name']}】重新检测正常"})
    return recovered


async def remove_replaced_stalled(pending: list[dict[str, Any]]) -> int:
    """删掉已被新资源取代的停滞旧种子（Transmission 里的任务）；文件只在与新种子不是同一发布时一并删除。"""
    replaced = await replaced_stalled_torrents(pending)
    if not replaced:
        return 0
    client = TransmissionClient()
    removed = 0
    for keep_data in (True, False):
        group = [entry for entry in replaced if entry["keep_data"] is keep_data]
        if not group:
            continue
        try:
            await asyncio.wait_for(client.remove_torrents([entry["hash"] for entry in group], delete_data=not keep_data), timeout=20)
        except Exception as exc:
            event_logger().warning("sync_stalled_remove_failed", extra={"error": safe_error(exc)})
            continue
        removed += len(group)
        for entry in group:
            event_logger().info(
                "sync_stalled_removed",
                extra={"hash": entry["hash"][:8], "detail": f"已删除被新资源取代的停滞任务：{sanitize_sensitive_text(entry['name'], 120)}"},
            )
    if removed:
        invalidate_downloads_snapshot()
    return removed


async def _run() -> dict[str, Any]:
    started = time.monotonic()
    # 缓存在原地刷新：先读到新数据再替换，同时到达的页面请求读到的永远是完整的缓存，不必自己再去读一遍。
    sources: dict[str, dict[str, Any]] = {}
    try:
        sources["transmission"], torrents = await _check_transmission()
    except Exception as exc:
        sources["transmission"], torrents = _failed("Transmission", exc), []
    sources["moviepilot"] = await _check_moviepilot()
    if sources["moviepilot"]["ok"]:
        await organize_failures(force=True)
    sources["emby"] = await _check_emby()

    # 不是经 AutoList 提交的种子：识别出是哪部影片并缓存，片单影片才能对上它。
    if torrents and sources["moviepilot"]["ok"]:
        known = _known_hashes()
        unknown = [torrent for torrent in torrents if str(torrent.get("hashString") or "").strip().casefold() not in known]
        try:
            await asyncio.wait_for(identify_torrents(unknown), timeout=IDENTIFY_TIMEOUT_SECONDS)
        except Exception as exc:
            event_logger().warning("sync_identify_failed", extra={"error": safe_error(exc)})

    try:
        await retest_failing_sites()
    except Exception as exc:
        event_logger().warning("sync_site_retest_failed", extra={"error": safe_error(exc)})

    downloading = 0
    arrived = 0
    removed = 0
    try:
        with connect() as conn:
            pending = [item for item in film_queries.playlist_items(conn, None) if item.get("library_state") != "in_library"]
        if sources["transmission"]["ok"]:
            removed = await remove_replaced_stalled(pending)
        projected = await project_films(pending)
        downloading_ids = [film["id"] for film in projected if film["status"] == "downloading"]
        downloading = len(downloading_ids)
        # 正在下载的影片每次都向 Emby 复查；其余未入馆的影片按全量复查的间隔。
        if sources["emby"]["ok"]:
            arrived += await recheck_library_states(downloading_ids)
            if library_recheck_due():
                arrived += await recheck_library_states()
    except Exception as exc:
        event_logger().warning("sync_reconcile_failed", extra={"error": safe_error(exc)})
    if arrived:
        event_logger().info("sync_library_arrived", extra={"detail": f"同步：{arrived} 部影片已入馆", "total": arrived})
    return {
        "ran_at": utc_now(), "duration_ms": int((time.monotonic() - started) * 1000),
        "sources": sources, "downloading": downloading, "arrived": arrived, "removed": removed,
    }


async def reconcile() -> dict[str, Any]:
    """立即同步一次；已有同步在进行时等待它并共用结果。"""
    global _inflight, _last_started, _last_result
    loop = asyncio.get_running_loop()
    if _inflight is None or _inflight.done() or _inflight.get_loop() is not loop:
        _last_started = time.monotonic()
        _inflight = loop.create_task(_run())
    task = _inflight
    result = await asyncio.shield(task)
    _last_result = result
    return result


def running() -> bool:
    return _inflight is not None and not _inflight.done()


async def wait() -> None:
    """等正在进行的同步结束（没有则立即返回）。"""
    if _inflight is not None and not _inflight.done():
        await asyncio.wait({_inflight})


def sync_due() -> bool:
    return _last_started is None or time.monotonic() - _last_started >= SYNC_INTERVAL_SECONDS


def kick() -> None:
    """打开首页时在后台同步一次（距上次不足 KICK_MIN_GAP_SECONDS 或正在同步则不重复）。"""
    if _inflight is not None and not _inflight.done():
        return
    if _last_started is not None and time.monotonic() - _last_started < KICK_MIN_GAP_SECONDS:
        return

    async def run() -> None:
        try:
            await reconcile()
        except Exception as exc:  # 同步失败不影响首页
            event_logger().warning("sync_kick_failed", extra={"error": safe_error(exc)})

    task = asyncio.get_running_loop().create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)


def status() -> dict[str, Any]:
    return {
        "running": _inflight is not None and not _inflight.done(),
        "last": _last_result,
        "interval_seconds": int(SYNC_INTERVAL_SECONDS),
    }
