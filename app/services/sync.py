"""统一同步：把 Transmission、MoviePilot、Emby 三方的最新情况对齐到片单影片。

影片状态本身由 ``films.project_films`` 实时计算；这里负责让它读到的数据是新的：
刷新 Transmission 快照与 MoviePilot 整理结果的缓存，识别 Transmission 里不是经 AutoList 提交的种子
（手动添加的也对得上片单影片），并立即向 Emby 复查正在下载的影片。三处共用同一个入口，
定时任务、打开首页与“立即同步”按钮都走 ``reconcile``，同一时刻只会有一次在跑。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..clients import EmbyClient, MoviePilotClient, TransmissionClient
from ..config import settings
from ..database import connect
from ..logs import event_logger
from ..queries import films as film_queries
from ..security import safe_error, sanitize_sensitive_text
from ..util import utc_now
from .downloads import enrich_torrent_media, identify_torrents
from .films import clear_stale_candidates, items_with_usable_candidates, project_films, replaced_stalled_torrents
from .history import organize_failures
from .identity import verify_waiting_identities
from .recognition import backfill_chinese_titles
from .library import library_recheck_due, recheck_library_states
from .search import begin_search_task_slot, fetch_downloads_now, invalidate_downloads_snapshot, run_search
from .sites import test_site_config
from ..queries.search import insert_search_task, item_in_active_search
from ..queries.sites import searchable_site_ids
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
    try:
        torrents = await fetch_downloads_now()
    except Exception as exc:
        # 读不到实时数据时本轮不做依赖它的自动操作（删除被取代的停滞任务、自动重新寻片）；
        # 页面展示仍可沿用最近一次的结果。
        reason = "读取超时" if isinstance(exc, TimeoutError) or not str(exc) else safe_error(exc)
        return _source(True, False, f"无法读取 Transmission 的下载列表：{reason}"), []
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
            """SELECT * FROM pt_sites WHERE enabled=1
                 AND ((search_enabled=1 AND last_status IN ('error','empty')) OR auto_paused_at IS NOT NULL)
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


async def remove_replaced_stalled(pending: list[dict[str, Any]], torrents: list[dict[str, Any]]) -> int:
    """删掉已被新资源取代的停滞旧种子（Transmission 里的任务）；文件只在与新种子不是同一发布时一并删除。"""
    replaced = await replaced_stalled_torrents(pending, torrents)
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


# 下载停滞、而且手上的候选都已过期（或根本没有）的影片，自动重新寻片一次：每次同步最多启动几部，同一部影片隔这么久才再试。
STALLED_RESEARCH_PER_RUN = 3
STALLED_RESEARCH_COOLDOWN = timedelta(hours=24)


def _recently_researched(conn: Any) -> set[int]:
    cutoff = (datetime.now(timezone.utc) - STALLED_RESEARCH_COOLDOWN).isoformat()
    ids: set[int] = set()
    for row in conn.execute("SELECT item_ids_json FROM search_tasks WHERE trigger='stalled' AND created_at>=?", (cutoff,)).fetchall():
        try:
            ids.update(int(value) for value in json.loads(row["item_ids_json"] or "[]"))
        except (TypeError, ValueError):
            continue
    return ids


def _start_film_search(item: dict[str, Any], trigger: str) -> bool | None:
    """为一部影片启动一次单片寻片。成功返回 True；暂时启动不了（并发已满等）返回 None，调用方应停止本轮；没有可搜的站点返回 False。"""
    item_id = int(item["id"])
    try:
        with connect() as conn:
            begin_search_task_slot(conn)
            site_ids = searchable_site_ids(conn)
            if not site_ids:
                return False
            rank = int(item.get("rank_no") or 0)
            task_id = insert_search_task(
                conn, item["playlist_id"], range_start=rank, range_end=rank, trigger=trigger, site_ids=site_ids, item_ids=[item_id],
            )
    except Exception as exc:  # 已达并发上限等：下一次同步再试
        event_logger().info("sync_auto_search_skipped", extra={"error": safe_error(exc)})
        return None
    SEARCH.start(task_id, run_search(task_id))
    event_logger().info(
        "sync_stalled_research" if trigger == "stalled" else "sync_waiting_research",
        extra={
            "detail": ("下载停滞且没有可用候选，已自动重新寻片：" if trigger == "stalled" else "等待资源的影片到了复查时间，已自动重新寻片：")
            + sanitize_sensitive_text(str(item.get("original_title") or ""), 80),
            "task_id": task_id,
        },
    )
    return True


async def research_stalled(projected: list[dict[str, Any]], pending: list[dict[str, Any]]) -> int:
    """停滞的下载没有可用的候选可换时，自动为这部影片重新寻片；返回启动的任务数。"""
    stalled = {film["id"] for film in projected if film["status"] == "downloading" and "download_stalled" in film["issues"]}
    if not stalled:
        return 0
    items = [item for item in pending if item["id"] in stalled]
    usable = items_with_usable_candidates(items)
    started = 0
    for item in items:
        if started >= STALLED_RESEARCH_PER_RUN:
            break
        item_id = int(item["id"])
        if item_id in usable:
            continue
        with connect() as conn:
            if item_id in _recently_researched(conn) or item_in_active_search(conn, item["playlist_id"], item_id):
                continue
        result = _start_film_search(item, "stalled")
        if result is not True:
            break
        started += 1
    return started


# 等待资源的影片（连续没有合格资源）到了复查时间，自动再搜一次；每次同步最多启动几部。
WAITING_RESEARCH_PER_RUN = 2


async def research_waiting(projected: list[dict[str, Any]], pending: list[dict[str, Any]]) -> int:
    """到了复查时间的“等待资源”影片自动重新寻片；返回启动的任务数。"""
    now = datetime.now(timezone.utc).isoformat()
    missing = {film["id"] for film in projected if film["status"] == "missing"}
    due = sorted(
        (item for item in pending if item["id"] in missing and item.get("next_search_at") and str(item["next_search_at"]) <= now),
        key=lambda item: str(item["next_search_at"]),
    )
    started = 0
    for item in due:
        if started >= WAITING_RESEARCH_PER_RUN:
            break
        with connect() as conn:
            if item_in_active_search(conn, item["playlist_id"], int(item["id"])):
                continue
        result = _start_film_search(item, "waiting")
        if result is not True:
            break
        started += 1
    return started


async def _run() -> dict[str, Any]:
    started = time.monotonic()
    # 缓存在原地刷新：先读到新数据再替换，同时到达的页面请求读到的永远是完整的缓存，不必自己再去读一遍。
    sources: dict[str, dict[str, Any]] = {}
    # 三个系统互不依赖，同时检查：Transmission 忙的时候读一次要十来秒，不必让 MoviePilot 与 Emby 排在它后面。
    transmission_result, sources["moviepilot"], sources["emby"] = await asyncio.gather(
        _check_transmission(), _check_moviepilot(), _check_emby(), return_exceptions=True,
    )
    if isinstance(transmission_result, BaseException):
        sources["transmission"], torrents = _failed("Transmission", transmission_result), []  # type: ignore[arg-type]
    else:
        sources["transmission"], torrents = transmission_result
    for name, label in (("moviepilot", "MoviePilot"), ("emby", "Emby")):
        if isinstance(sources[name], BaseException):
            sources[name] = _failed(label, sources[name])  # type: ignore[arg-type]
    if sources["moviepilot"]["ok"]:
        await organize_failures(force=True)

    # 不是经 AutoList 提交的种子：识别出是哪部影片并缓存，片单影片才能对上它。
    if torrents and sources["moviepilot"]["ok"]:
        known = _known_hashes()
        unknown = [torrent for torrent in torrents if str(torrent.get("hashString") or "").strip().casefold() not in known]
        try:
            await asyncio.wait_for(identify_torrents(unknown), timeout=IDENTIFY_TIMEOUT_SECONDS)
            await asyncio.wait_for(
                enrich_torrent_media([str(torrent.get("hashString") or "").strip().casefold() for torrent in unknown]),
                timeout=IDENTIFY_TIMEOUT_SECONDS,
            )
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
        cleared = clear_stale_candidates()
        if cleared:
            event_logger().info("sync_stale_candidates_cleared", extra={"detail": f"{cleared} 部影片的候选超过 7 天没选定，已自动清除，回到缺片"})
        with connect() as conn:
            pending = [item for item in film_queries.playlist_items(conn, None) if item.get("library_state") != "in_library"]
        if sources["transmission"]["ok"]:
            removed = await remove_replaced_stalled(pending, torrents)
        projected = await project_films(pending)
        if sources["transmission"]["ok"]:
            await research_stalled(projected, pending)
        await research_waiting(projected, pending)
        waiting_ids = {film["id"] for film in projected if film["status"] == "missing" and "no_eligible" in film["issues"]}
        flagged = await verify_waiting_identities([item for item in pending if item["id"] in waiting_ids])
        localized = await backfill_chinese_titles()
        if localized:
            event_logger().info("sync_titles_localized", extra={"detail": f"{localized} 部影片的 TMDB 简体中文片名为空，已改用新加坡 / 香港 / 台湾的中文译名"})
        if flagged:
            event_logger().info("sync_identity_flagged", extra={"detail": f"{flagged} 部等待资源的影片 TMDB 识别可能有误，已标记，请核对"})
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
