"""Playlist automation, incremental sync, and background scheduler."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from ..clients import EmbyClient
from ..config import settings
from ..database import cleanup_old_data, connect, json_value
from ..domain.titles import canonical_item_title, canonical_item_year, item_identity_keys
from .. import state
from ..logs import event_logger
from ..list_sources import PlaylistSourceFetcher
from ..security import safe_error, sanitize_sensitive_text
from ..tasks import AUTOMATION, LIBRARY, SEARCH
from ..util import to_int, utc_now
from .films import reidentify_item
from .library import library_details, run_library_scan
from .recognition import persist_tmdb_item, recognize_item
from .imports import normalize_import_items
from .search import begin_search_task_slot, run_search, searchable_playlist_items
from .cookiecloud import cookiecloud_configured, pull_cookiecloud, pull_due
from .sites import refresh_stale_site_account_stats


def _trigger_post_recognition_library_scan(playlist_id: int) -> None:
    """识别完成后自动刷新 Emby 状态；未配置 Emby 时不写入无法确认的入库状态。"""
    if not settings.emby_base_url or not settings.emby_api_key:
        return
    if LIBRARY.capacity_error() is not None:
        return
    try:
        lib_task_id: int | None = None
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT id FROM library_scan_tasks WHERE playlist_id=? AND status IN ('queued','running')",
                (playlist_id,),
            ).fetchone()
            if active:
                return
            total = conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (playlist_id,)).fetchone()[0]
            if not total:
                return
            cursor = conn.execute(
                "INSERT INTO library_scan_tasks(playlist_id,status,total,created_at,updated_at) VALUES(?,?,?,?,?)",
                (playlist_id, "queued", total, utc_now(), utc_now()),
            )
            lib_task_id = cursor.lastrowid
        if lib_task_id is not None:
            LIBRARY.start(lib_task_id, run_library_scan(lib_task_id))
    except Exception as exc:
        # 自动扫描只是识别后的附加步骤，失败不能改写已经完成的识别任务状态。
        event_logger().warning("library_scan_auto_trigger_failed", extra={"error": safe_error(exc)})


def update_recognition_task(task_id: int, **values: Any) -> None:
    if not set(values).issubset({"status", "completed", "matched", "corrected", "error_message"}):
        raise ValueError("无效的识别任务字段")
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in values)
    with connect() as conn:
        # Dynamic column names are restricted by the allowlist above.
        conn.execute(f"UPDATE recognition_tasks SET {assignments} WHERE id=?", (*values.values(), task_id))  # nosec B608


async def run_recognition(task_id: int) -> None:
    playlist_id: int | None = None
    try:
        with connect() as conn:
            task = conn.execute("SELECT * FROM recognition_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return
            playlist_id = int(task["playlist_id"])
            verify = task["mode"] == "verify"
            # 校准处理整份片单；普通识别只处理还没识别的影片。
            items = conn.execute(
                """SELECT * FROM playlist_items
                   WHERE playlist_id=? AND (? OR tmdb_id IS NULL OR tmdb_title IS NULL OR tmdb_original_title IS NULL)
                   ORDER BY rank_no""", (task["playlist_id"], int(verify)),
            ).fetchall()
        if items and not settings.tmdb_api_key:
            # 未配置 TMDB 时每部影片都会得到同一个错误，直接失败并给出配置提示。
            update_recognition_task(task_id, status="failed", error_message="请先在设置中填写 TMDB API Key")
            return
        update_recognition_task(task_id, status="running")
        matched, corrected, errors = 0, 0, []
        for completed, item in enumerate(items, start=1):
            try:
                media = await recognize_item(item)
                current = item["tmdb_id"]
                if media and current and to_int(media["id"]) != to_int(current):
                    # 已识别的影片只在按编号（IMDb / 来源 TMDB）得到不同结果时改正；片名搜索不推翻已有结果。
                    if media.get("matched_by") in {"imdb", "tmdb"}:
                        reidentify_item(int(item["id"]), media, item["imdb_id"])
                        corrected += 1
                        event_logger().info(
                            "recognition_corrected",
                            extra={"detail": f"#{item['rank_no']} TMDB {current} → {media['id']}（按 {media['matched_by']} 编号）"},
                        )
                    matched += 1
                elif media:
                    persist_tmdb_item(int(item["id"]), media, item["imdb_id"])
                    matched += 1
                else:
                    errors.append(f"#{item['rank_no']} 未识别")
            except Exception as exc:
                errors.append(f"#{item['rank_no']} {safe_error(exc)}")
            update_recognition_task(task_id, completed=completed, matched=matched, corrected=corrected)
        status = "partial" if errors else "completed"
        update_recognition_task(
            task_id, status=status, completed=len(items), matched=matched,
            error_message="；".join(errors[:8])[:500] if errors else None,
        )
        event_logger().info(
            "recognition_finished",
            extra={
                "task_id": task_id,
                "status": status,
                "total": len(items),
                "matched": matched,
                "detail": (
                    f"按 IMDb 校准结束：{len(items)} 部中改正 {corrected} 部" if verify
                    else f"TMDB 识别结束：{matched}/{len(items)} 部已识别"
                ),
            },
        )
        if playlist_id is not None:
            _trigger_post_recognition_library_scan(playlist_id)
    except asyncio.CancelledError as _cancel:
        update_recognition_task(task_id, status="cancelled")
        raise
    except Exception as exc:
        update_recognition_task(task_id, status="failed", error_message=safe_error(exc))


def update_automation_run(run_id: int, **values: Any) -> None:
    allowed = {"status", "stage", "total", "completed", "recognized", "searched", "recommended", "message"}
    if not set(values).issubset(allowed):
        raise ValueError("无效的自动化任务字段")
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in values)
    with connect() as conn:
        conn.execute(f"UPDATE automation_runs SET {assignments} WHERE id=?", (*values.values(), run_id))  # nosec B608


def add_notification(title: str, message: str, level: str = "info") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO notifications(level,title,message,created_at) VALUES(?,?,?,?)",
            (level, title[:120], sanitize_sensitive_text(message, 500), utc_now()),
        )


async def run_playlist_automation(run_id: int) -> None:
    try:
        with connect() as conn:
            run = conn.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
            playlist = conn.execute("SELECT * FROM playlists WHERE id=?", (run["playlist_id"],)).fetchone() if run else None
        if not run or not playlist:
            return
        queue = await searchable_playlist_items(int(playlist["id"]), int(playlist["automation_batch_size"] or 50))
        if queue.get("download_state") == "unknown":
            message = "下载器当前不可用，已暂停自动化处理；请检查 Transmission 连接后重试"
            update_automation_run(run_id, status="blocked", stage="blocked", message=message)
            add_notification("新增影片处理已暂停", message, "warning")
            return
        items = list(queue.get("items") or [])
        update_automation_run(run_id, status="running", stage="recognition", total=len(items))
        emby = EmbyClient()
        recognized = searched = recommended = 0
        searchable_ids: list[int] = []
        for completed, item in enumerate(items, start=1):
            tmdb_id = item["tmdb_id"]
            if not tmdb_id or not item["tmdb_title"] or not item["tmdb_original_title"]:
                media = await recognize_item(item)
                if media:
                    tmdb_id = int(media["id"])
                    recognized += 1
                    persist_tmdb_item(int(item["id"]), media, item["imdb_id"])
                    item = dict(item)
                    item.update({
                        "tmdb_title": media.get("title"), "tmdb_original_title": media.get("original_title"),
                        "tmdb_year": str(media.get("release_date") or "")[:4] or item["year"],
                        "tmdb_imdb_id": media.get("imdb_id") or item["imdb_id"],
                    })
            update_automation_run(run_id, stage="library", completed=completed, recognized=recognized)
            state, emby_item_id, image_tag = await library_details(
                emby, canonical_item_title(item), canonical_item_year(item), tmdb_id,
                item["tmdb_imdb_id"] or item["imdb_id"],
            )
            with connect() as conn:
                conn.execute(
                    "UPDATE playlist_items SET library_state=?,library_checked_at=?,emby_item_id=?,emby_image_tag=? WHERE id=?",
                    (state, utc_now(), emby_item_id, image_tag, item["id"]),
                )
            if state != "in_library" and tmdb_id:
                searchable_ids.append(int(item["id"]))
        search_incomplete = False
        final_task = None
        if searchable_ids:
            with connect() as conn:
                begin_search_task_slot(conn)
                ranks = conn.execute(
                    f"SELECT MIN(rank_no),MAX(rank_no) FROM playlist_items WHERE id IN ({','.join('?' for _ in searchable_ids)})",  # nosec B608
                    searchable_ids,
                ).fetchone()
                now = utc_now()
                site_ids = [
                    int(row["id"]) for row in conn.execute(
                        "SELECT id FROM pt_sites WHERE enabled=1 AND search_enabled=1 ORDER BY priority,id",
                    ).fetchall()
                ]
                if not site_ids:
                    raise HTTPException(422, "请先在站点配置中选择至少一个参与搜索的站点")
                task_cursor = conn.execute(
                    """INSERT INTO search_tasks(
                         playlist_id,range_start,range_end,status,total,trigger,site_ids_json,item_ids_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (playlist["id"], ranks[0], ranks[1], "queued", len(searchable_ids), "automation",
                     json_value(site_ids), json_value(searchable_ids), now, now),
                )
                task_lastrowid = task_cursor.lastrowid
                if task_lastrowid is None:
                    raise RuntimeError("搜索任务写入失败")
                task_id = task_lastrowid
            update_automation_run(run_id, stage="search")
            # 独立搜索任务句柄（审计 3-9）：取消搜索任务时，CancelledError 会从
            # await 传播到本协程，run 被标记 cancelled 并终止——这是有意的传播
            # 语义（用户取消搜索即取消该批自动化处理）；任务结束后由任务框架清理登记。
            await SEARCH.start(task_id, run_search(task_id))
            # searched 以搜索任务实际完成数为准，而非计划数（任务可能 failed/partial）。
            # cancelled 不会到达这里（CancelledError 已由上层 except 处理）。
            with connect() as conn:
                final_task = conn.execute("SELECT status,completed FROM search_tasks WHERE id=?", (task_id,)).fetchone()
            # 搜索任务的 partial/failed/interrupted 都表示本轮没有完整覆盖站点，
            # 自动化 run 必须保留为警告状态，不能把部分结果包装成成功。
            search_incomplete = not final_task or final_task["status"] != "completed"
            searched = int(final_task["completed"] or 0) if final_task else 0
            with connect() as conn:
                preferred_rows = conn.execute(
                    """SELECT c.id,c.playlist_item_id FROM candidates c
                       WHERE c.task_id=? AND c.recommendation='preferred' AND c.eligibility='eligible'
                       ORDER BY c.playlist_item_id,c.ranking""", (task_id,),
                ).fetchall()
                preferred = []
                seen_items: set[int] = set()
                for row in preferred_rows:
                    if int(row["playlist_item_id"]) not in seen_items:
                        preferred.append(row)
                        seen_items.add(int(row["playlist_item_id"]))
                recommended = len(preferred)
        final_task_status = final_task["status"] if final_task else "unknown"
        if search_incomplete:
            message = (
                f"处理 {len(items)} 部，新增识别 {recognized}，搜索 {searched}；"
                f"搜索任务 {final_task_status}，失败站点需重试"
            )
            update_automation_run(
                run_id, status="partial", stage="search", completed=len(items), recognized=recognized,
                searched=searched, recommended=recommended, message=message,
            )
            add_notification("新增影片处理部分完成", message, "warning")
        else:
            message = f"处理 {len(items)} 部，新增识别 {recognized}，搜索 {searched}，推荐 {recommended}；候选需人工确认"
            update_automation_run(
                run_id, status="completed", stage="completed", completed=len(items), recognized=recognized,
                searched=searched, recommended=recommended, message=message,
            )
            add_notification("新增影片处理完成", message, "success")
    except HTTPException as exc:
        if exc.status_code == 429:
            # 搜索任务容量已满：任务回到排队状态，由调度器在容量释放后继续消费。
            update_automation_run(run_id, status="queued", stage="queued", message="搜索任务容量已满，等待空闲后自动继续")
            return
        reason = safe_error(exc)
        update_automation_run(run_id, status="failed", message=reason)
        add_notification("新增影片处理失败", reason, "error")
    except asyncio.CancelledError as _cancel:
        update_automation_run(run_id, status="cancelled", message="新片处理任务已取消")
        raise
    except Exception as exc:
        reason = safe_error(exc)
        update_automation_run(run_id, status="failed", message=reason)
        add_notification("新增影片处理失败", reason, "error")


_playlist_sync_locks: dict[int, asyncio.Lock] = {}


def playlist_sync_lock(playlist_id: int) -> asyncio.Lock:
    """进程内片单同步锁：手动刷新与定时增量共享，统一所有 rank 写入路径（审计 3-3）。"""
    try:
        key = int(playlist_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("片单 ID 无效") from exc
    return _playlist_sync_locks.setdefault(key, asyncio.Lock())


async def sync_playlist_incremental(playlist_id: int, trigger: str = "manual") -> dict[str, Any]:
    """Serialize source fetch and rank allocation per playlist.

    The network fetch intentionally happens while holding this lock: otherwise
    a manual request and the scheduler can both read the same ``max(rank_no)``
    and race on the playlist's unique rank constraint.
    """
    lock = playlist_sync_lock(playlist_id)
    if lock.locked():
        raise HTTPException(409, "该片单已有同步任务运行，请等待完成")
    async with lock:
        return await _sync_playlist_incremental(playlist_id, trigger)


async def _sync_playlist_incremental(playlist_id: int, trigger: str = "manual") -> dict[str, Any]:
    with connect() as conn:
        playlist = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
    if not playlist:
        raise HTTPException(404, "片单不存在")
    if not playlist["source_url"]:
        raise HTTPException(422, "该片单没有可同步的网址来源")
    try:
        source = await PlaylistSourceFetcher().fetch(str(playlist["source_url"]), 10000)
        incoming = normalize_import_items(source.get("items", []))
        with connect() as conn:
            existing = conn.execute(
                """SELECT id,imdb_id,tmdb_id,source_tmdb_id,source_ref,original_title,year
                   FROM playlist_items WHERE playlist_id=?""", (playlist_id,),
            ).fetchall()
            keys = {
                key
                for row in existing
                for key in item_identity_keys(dict(row))
            }
            by_key = {key: row for row in existing for key in item_identity_keys(dict(row))}
            max_rank = int(conn.execute("SELECT COALESCE(MAX(rank_no),0) FROM playlist_items WHERE playlist_id=?", (playlist_id,)).fetchone()[0])
            additions = []
            for item in incoming:
                if any(key in keys for key in item_identity_keys(item)):
                    # 增量同步不改动已有影片，只补上早期导入时没有保存的来源身份。
                    old = next(by_key[key] for key in item_identity_keys(item) if key in by_key)
                    conn.execute(
                        """UPDATE playlist_items SET source_tmdb_id=COALESCE(source_tmdb_id,?),source_ref=COALESCE(source_ref,?)
                           WHERE id=?""",
                        (item.get("tmdb_id"), item.get("source_ref"), old["id"]),
                    )
                    continue
                keys.update(item_identity_keys(item))
                max_rank += 1
                additions.append({**item, "playlist_id": playlist_id, "rank_no": max_rank})
            if additions:
                conn.executemany(
                    """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,source_tmdb_id,source_ref)
                       VALUES(:playlist_id,:rank_no,:imdb_id,:original_title,:year,:chinese_title,:tmdb_id,:source_ref)""", additions,
                )
            next_sync = (datetime.now(timezone.utc) + timedelta(hours=int(playlist["sync_interval_hours"] or 24))).isoformat()
            message = f"增量同步完成，新增 {len(additions)} 部，保留现有 {len(existing)} 部"
            conn.execute(
                """UPDATE playlists SET source_name=?,last_synced_at=?,next_sync_at=?,last_sync_status='completed',last_sync_message=?
                   WHERE id=?""", (source.get("source_name"), utc_now(), next_sync, message, playlist_id),
            )
        add_notification("片单来源已同步", f"{playlist['name']}：{message}", "success")
        event_logger().info(
            "playlist_synced",
            extra={
                "detail": f"片单【{playlist['name']}】{message}",
                "trigger": trigger,
                "total": len(additions),
            },
        )
        if additions and playlist["automation_enabled"]:
            await start_playlist_automation(playlist_id, "sync")
        return {"id": playlist_id, "added": len(additions), "message": message, "trigger": trigger}
    except HTTPException:
        raise
    except Exception as exc:
        reason = safe_error(exc)
        with connect() as conn:
            conn.execute(
                "UPDATE playlists SET last_sync_status='failed',last_sync_message=?,next_sync_at=? WHERE id=?",
                (reason, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), playlist_id),
            )
        add_notification("片单同步失败", f"{playlist['name']}：{reason}", "error")
        event_logger().error(
            "playlist_sync_failed",
            extra={
                "detail": f"片单【{playlist['name']}】同步失败：{reason}",
                "trigger": trigger,
                "error": reason,
            },
        )
        raise


def _automation_capacity_full() -> bool:
    return AUTOMATION.capacity_error() is not None


def _claim_automation_run(run_id: int) -> bool:
    """Atomically claim a queued run before creating its process-local task.

    The partial unique index prevents duplicate active runs per playlist; this
    conditional update additionally prevents two scheduler instances from
    starting the same queued row at the same time.
    """
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """UPDATE automation_runs
               SET status='running',stage='queued',updated_at=?
               WHERE id=? AND status='queued'""", (utc_now(), run_id),
        ).rowcount
    return updated == 1


def _active_automation_run(playlist_id: int) -> Any:
    with connect() as conn:
        return conn.execute(
            """SELECT id,status FROM automation_runs
               WHERE playlist_id=? AND status IN ('queued','running')
               ORDER BY id DESC LIMIT 1""", (playlist_id,),
        ).fetchone()


async def start_playlist_automation(playlist_id: int, trigger: str = "manual") -> dict[str, Any]:
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            playlist = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
            if not playlist:
                raise HTTPException(404, "片单不存在")
            active = conn.execute(
                """SELECT id,status FROM automation_runs
                   WHERE playlist_id=? AND status IN ('queued','running')
                   ORDER BY id DESC LIMIT 1""", (playlist_id,),
            ).fetchone()
            if active:
                status = str(active["status"])
                return {
                    "id": int(active["id"]), "status": status,
                    "message": "新增影片处理任务已排队" if status == "queued" else "新增影片处理任务正在运行",
                }
            now = utc_now()
            run_cursor = conn.execute(
                "INSERT INTO automation_runs(playlist_id,trigger,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (playlist_id, trigger, "queued", "queued", now, now),
            )
            run_lastrowid = run_cursor.lastrowid
            if run_lastrowid is None:
                raise RuntimeError("自动化任务写入失败")
            run_id = run_lastrowid
    except sqlite3.IntegrityError as _integrity:
        # A second process may have won the unique-index race between its
        # transaction and ours.  Return the existing run instead of 500.
        active = _active_automation_run(playlist_id)
        if active:
            status = str(active["status"])
            return {
                "id": int(active["id"]), "status": status,
                "message": "新增影片处理任务已排队" if status == "queued" else "新增影片处理任务正在运行",
            }
        raise
    if _automation_capacity_full():
        # 容量满时不报错：任务保持 queued，由调度器在容量释放后自动启动。
        return {"id": run_id, "status": "queued", "message": "自动化任务已排队，容量释放后自动执行"}
    if _claim_automation_run(run_id):
        AUTOMATION.start(run_id, run_playlist_automation(run_id))
    # claim 成功后数据库状态已是 running，返回值必须与真实状态一致。
    return {"id": run_id, "status": "running", "message": "已开始识别并搜索未入库影片；不会自动下载"}


async def consume_queued_automation_runs() -> int:
    """Start queued automation runs once capacity frees up; called by the scheduler.
    Runs already tracked in AUTOMATION.running are skipped so a freshly
    inserted-but-not-yet-running run is never started twice.
    """
    started = 0
    with connect() as conn:
        queued = conn.execute(
            "SELECT id,playlist_id FROM automation_runs WHERE status='queued' ORDER BY id LIMIT 8",
        ).fetchall()
    for row in queued:
        try:
            run_id = int(row["id"])
        except (TypeError, ValueError) as exc:
            event_logger().error("automation_run_invalid_id", extra={"error": safe_error(exc)})
            continue
        if run_id in AUTOMATION.running or _automation_capacity_full():
            continue
        if not _claim_automation_run(run_id):
            continue
        AUTOMATION.start(run_id, run_playlist_automation(run_id))
        started += 1
    return started


SCHEDULER_HEARTBEAT_INTERVAL_SECONDS = 30.0


async def _scheduler_heartbeat_loop() -> None:
    """Keep the liveness heartbeat independent from a potentially long tick.

    A source refresh, account probe, or cleanup operation can legitimately take
    longer than the health timeout.  The scheduler task is still alive in that
    case, so heartbeat updates must not depend on the tick reaching its next
    loop iteration.
    """
    while True:
        state.mark_scheduler_heartbeat(utc_now())
        await asyncio.sleep(SCHEDULER_HEARTBEAT_INTERVAL_SECONDS)


async def sync_scheduler() -> None:
    state.mark_scheduler_started(utc_now())
    heartbeat_task = asyncio.create_task(_scheduler_heartbeat_loop())
    try:
        while True:
            state.mark_scheduler_heartbeat(utc_now())
            tick_errors: list[str] = []
            try:
                # Run the synchronous cleanup off the event loop so it cannot
                # starve the independent heartbeat during a slow SQLite pass.
                await asyncio.to_thread(cleanup_old_data)
                await refresh_stale_site_account_stats()
                # 定时从 CookieCloud 拉取站点 Cookie（间隔见 cookiecloud.PULL_INTERVAL_SECONDS）。
                if cookiecloud_configured() and pull_due():
                    try:
                        applied = await pull_cookiecloud("schedule")
                        if applied["updated"]:
                            event_logger().info(
                                "scheduler_cookiecloud_synced",
                                extra={
                                    "detail": f"定时拉取 CookieCloud，已更新 {len(applied['updated'])} 个站点 Cookie",
                                    "total": len(applied["updated"]),
                                },
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        event_logger().warning("scheduler_cookiecloud_sync_failed", extra={"error": safe_error(exc)})
                await consume_queued_automation_runs()
                now = utc_now()
                with connect() as conn:
                    due = [int(row["id"]) for row in conn.execute(
                        """SELECT id FROM playlists WHERE sync_enabled=1 AND source_url IS NOT NULL AND source_url!=''
                           AND (next_sync_at IS NULL OR next_sync_at<=?)""", (now,),
                    ).fetchall()]
                for playlist_id in due:
                    try:
                        await sync_playlist_incremental(playlist_id, "schedule")
                    except asyncio.CancelledError as _cancel:
                        raise
                    except Exception as exc:
                        reason = safe_error(exc)
                        tick_errors.append(reason)
                        event_logger().warning("scheduler_playlist_sync_failed", extra={"error": reason})
            except asyncio.CancelledError as _cancel:
                raise
            except Exception as exc:
                reason = safe_error(exc)
                tick_errors.append(reason)
                event_logger().error("scheduler_tick_failed", extra={"error": reason})
            if tick_errors:
                state.mark_scheduler_error("；".join(tick_errors[:3]))
            else:
                state.mark_scheduler_success(utc_now())
            await asyncio.sleep(60)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        state.mark_scheduler_stopped()
