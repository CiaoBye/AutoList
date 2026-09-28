"""AutoList HTTP routes."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..clients import EmbyClient
from ..config import settings
from ..database import connect, json_value
from ..list_sources import PlaylistSourceFetcher
from ..logs import event_logger
from ..schemas import (
    ImportPayload,
    PlaylistAutomationPayload,
    PlaylistOrderPayload,
    PlaylistSyncPayload,
    PlaylistUpdatePayload,
)
from ..security import safe_error
from ..services.automation import playlist_sync_lock, run_recognition, start_playlist_automation, sync_playlist_incremental
from ..domain.titles import item_identity_keys
from ..services.history import playlist_item_snapshot
from ..services.imports import normalize_import_items, resolve_import
from ..services.library import run_library_scan
from ..state import (
    MAX_RUNNING_LIBRARY_TASKS,
    MAX_RUNNING_RECOGNITION_TASKS,
    enforce_background_task_capacity,
    poster_cache,
    remember_poster,
    running_automation_tasks,
    running_library_tasks,
    running_recognition_tasks,
    running_tasks,
)
from ..util import raster_image_media_type, rows_to_dicts, to_int, utc_now

router = APIRouter()


@router.get("/api/playlist-items/{playlist_item_id}/poster")
async def playlist_item_poster(playlist_item_id: int, tag: str = "") -> Response:
    with connect() as conn:
        row = conn.execute(
            "SELECT emby_item_id,emby_image_tag FROM playlist_items WHERE id=?", (playlist_item_id,),
        ).fetchone()
    if not row or not row["emby_item_id"]:
        raise HTTPException(404, "影片没有可用的 Emby 海报")
    emby_item_id = str(row["emby_item_id"])
    if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", emby_item_id):
        raise HTTPException(404, "Emby 影片标识无效")
    image_tag = str(row["emby_image_tag"] or tag or "")
    cache_key = f"{emby_item_id}:{image_tag}"
    if cache_key in poster_cache:
        content, media_type = poster_cache[cache_key]
    else:
        try:
            content, _ = await EmbyClient().poster(emby_item_id)
        except httpx.HTTPStatusError as exc:
            status = 404 if exc.response.status_code == 404 else 502
            raise HTTPException(status, "Emby 海报读取失败") from exc
        except Exception as exc:
            raise HTTPException(502, f"Emby 海报读取失败：{safe_error(exc)}") from exc
        media_type = raster_image_media_type(content) or ""
        if not media_type or len(content) > 8 * 1024 * 1024:
            raise HTTPException(422, "Emby 返回的海报格式无效")
        remember_poster(cache_key, (content, media_type))
    return Response(
        content=content, media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )

@router.post("/api/playlists/import/preview")
async def preview_playlist_import(payload: ImportPayload) -> dict[str, Any]:
    try:
        source_name, items, source = await resolve_import(payload)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(422, f"片单获取失败：{safe_error(exc)}") from exc
    if not items:
        raise HTTPException(422, "没有获取到可导入的电影")
    return {"name": payload.name or source_name, "count": len(items), "source_type": source.get("source_type"), "sample": items[:8]}

@router.post("/api/playlists/import")
async def import_playlist(payload: ImportPayload) -> dict[str, Any]:
    try:
        source_name, items, source = await resolve_import(payload)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(422, f"片单获取失败：{safe_error(exc)}") from exc
    if not items:
        raise HTTPException(422, "没有可导入的影片")
    name = payload.name or source_name or f"片单 {datetime.now().strftime('%Y-%m-%d')}"
    with connect() as conn:
        position = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM playlists").fetchone()[0]
        cursor = conn.execute(
            "INSERT INTO playlists(name,position,source_type,source_url,source_name,last_synced_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (name, position, source.get("source_type"), source.get("source_url"), source.get("source_name"), utc_now(), utc_now()),
        )
        playlist_id = cursor.lastrowid
        # 来源自带的 TMDB 编号只记为 source_tmdb_id，识别结果 tmdb_id 由识别任务写入。
        conn.executemany(
            """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,source_tmdb_id,source_ref)
               VALUES(:playlist_id,:rank_no,:imdb_id,:original_title,:year,:chinese_title,:tmdb_id,:source_ref)""",
            [{"playlist_id": playlist_id, "tmdb_id": None, "source_ref": None, **item} for item in items],
        )
        # 导入后自动识别只是便利步骤：未配置 TMDB 或识别容量已满时跳过，并把原因返回给界面。
        task_id = None
        recognition_note = None
        if not settings.tmdb_api_key:
            recognition_note = "未配置 TMDB API Key，已跳过自动识别"
        else:
            recognition_note = enforce_background_task_capacity(
                running_recognition_tasks, MAX_RUNNING_RECOGNITION_TASKS, "识别",
            )
            if recognition_note is None:
                task_cursor = conn.execute(
                    "INSERT INTO recognition_tasks(playlist_id,status,total,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (playlist_id, "queued", len(items), utc_now(), utc_now()),
                )
                task_id = task_cursor.lastrowid
    if task_id is not None:
        running_recognition_tasks[task_id] = asyncio.create_task(run_recognition(task_id))
    event_logger().info(
        "playlist_imported",
        extra={"total": len(items), "detail": f"导入片单【{name}】，共 {len(items)} 部影片"},
    )
    return {
        "id": playlist_id, "name": name, "count": len(items),
        "recognition_task_id": task_id, "recognition_note": recognition_note,
    }

@router.post("/api/playlists/{playlist_id}/refresh-source")
async def refresh_playlist_source(playlist_id: int) -> dict[str, Any]:
    # 与定时增量同步共享进程内锁，避免并发 rank 写入竞争唯一约束（审计 3-3）。
    lock = playlist_sync_lock(playlist_id)
    if lock.locked():
        raise HTTPException(409, "该片单已有同步任务运行，请等待完成")
    async with lock:
        return await _refresh_playlist_source_unlocked(playlist_id)


async def _refresh_playlist_source_unlocked(playlist_id: int) -> dict[str, Any]:
    with connect() as conn:
        playlist = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if not playlist:
            raise HTTPException(404, "片单不存在")
        if not playlist["source_url"]:
            raise HTTPException(422, "该片单不是通过网址导入的")
        active_tables = (
            ("search_tasks", "搜索"),
            ("recognition_tasks", "识别"),
            ("library_scan_tasks", "入库检查"),
        )
        for table, label in active_tables:
            if conn.execute(f"SELECT 1 FROM {table} WHERE playlist_id=? AND status IN ('queued','running')", (playlist_id,)).fetchone():  # nosec B608
                raise HTTPException(409, f"片单仍有{label}任务运行，请完成后再刷新")
        if conn.execute(
            "SELECT 1 FROM automation_runs WHERE playlist_id=? AND status IN ('queued','running')",
            (playlist_id,),
        ).fetchone():
            raise HTTPException(409, "片单仍有自动化任务运行，请完成后再刷新")
    try:
        source = await PlaylistSourceFetcher().fetch(str(playlist["source_url"]), 10000)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(422, f"片单刷新失败：{safe_error(exc)}") from exc
    items = normalize_import_items(source.get("items", []))
    if not items:
        raise HTTPException(422, "来源没有返回可用电影，已保留现有片单")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        playlist = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if not playlist:
            raise HTTPException(404, "片单不存在")
        active_tables = (
            ("search_tasks", "搜索"),
            ("recognition_tasks", "识别"),
            ("library_scan_tasks", "入库检查"),
        )
        for table, label in active_tables:
            if conn.execute(f"SELECT 1 FROM {table} WHERE playlist_id=? AND status IN ('queued','running')", (playlist_id,)).fetchone():  # nosec B608
                raise HTTPException(409, f"片单刷新期间启动了{label}任务，已保留现有片单")
        if conn.execute(
            "SELECT 1 FROM automation_runs WHERE playlist_id=? AND status IN ('queued','running')",
            (playlist_id,),
        ).fetchone():
            raise HTTPException(409, "片单刷新期间启动了自动化任务，已保留现有片单")
        existing_rows = list(conn.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY rank_no", (playlist_id,)).fetchall())

        indexed: dict[tuple[str, str], list[Any]] = {}
        for old in existing_rows:
            for key in item_identity_keys(dict(old)):
                indexed.setdefault(key, []).append(old)
        used_ids: set[int] = set()
        matched_ids: set[int] = set()
        rank_offset = max([to_int(row["rank_no"]) for row in existing_rows] + [len(items), 1]) + len(existing_rows) + 1
        conn.execute("UPDATE playlist_items SET rank_no=rank_no+? WHERE playlist_id=?", (rank_offset, playlist_id))
        for item in items:
            matches = [
                old for key in item_identity_keys(item)
                for old in indexed.get(key, [])
                if to_int(old["id"]) not in used_ids
            ]
            old = matches[0] if matches else None
            if old:
                old_id = to_int(old["id"])
                used_ids.add(old_id)
                matched_ids.add(old_id)
                incoming_tmdb_id = item.get("tmdb_id")
                source_tmdb_id = incoming_tmdb_id if incoming_tmdb_id is not None else old["source_tmdb_id"]
                source_ref = item.get("source_ref") or old["source_ref"]
                # 来源给出的 TMDB 编号与已识别结果不一致：以来源为准，清空识别结果交给识别任务重做。
                tmdb_changed = incoming_tmdb_id is not None and old["tmdb_id"] not in (None, incoming_tmdb_id)
                if tmdb_changed:
                    conn.execute(
                        """UPDATE playlist_items SET rank_no=?,imdb_id=?,original_title=?,year=?,chinese_title=?,
                                  source_tmdb_id=?,source_ref=?,tmdb_id=NULL,
                                  tmdb_title=NULL,tmdb_original_title=NULL,tmdb_year=NULL,tmdb_imdb_id=NULL,tmdb_checked_at=NULL,
                                  tmdb_poster_path=NULL,tmdb_original_language=NULL,fanart_poster_url=NULL,
                                  fanart_backdrop_url=NULL,tmdb_backdrop_path=NULL,
                                  library_state='unknown',library_checked_at=NULL,emby_item_id=NULL,emby_image_tag=NULL WHERE id=?""",
                        (item["rank_no"], item.get("imdb_id") or old["imdb_id"], item["original_title"], item.get("year"),
                         item.get("chinese_title"), source_tmdb_id, source_ref, old_id),
                    )
                else:
                    conn.execute(
                        """UPDATE playlist_items SET rank_no=?,imdb_id=?,original_title=?,year=?,chinese_title=?,
                                  source_tmdb_id=?,source_ref=? WHERE id=?""",
                        (item["rank_no"], item.get("imdb_id") or old["imdb_id"], item["original_title"], item.get("year"),
                         item.get("chinese_title"), source_tmdb_id, source_ref, old_id),
                    )
            else:
                new_cursor = conn.execute(
                    """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,source_tmdb_id,source_ref)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (playlist_id, item["rank_no"], item.get("imdb_id"), item["original_title"], item.get("year"),
                     item.get("chinese_title"), item.get("tmdb_id"), item.get("source_ref")),
                )
                new_id = new_cursor.lastrowid
                if new_id is None:
                    raise RuntimeError("片单条目写入失败")
                matched_ids.add(new_id)
        stale_ids = [to_int(row["id"]) for row in existing_rows if to_int(row["id"]) not in matched_ids]
        for old in existing_rows:
            old_id = to_int(old["id"])
            if old_id in matched_ids:
                continue
            snapshot = json_value(playlist_item_snapshot(dict(old)))
            conn.execute(
                """UPDATE download_history SET playlist_item_snapshot_json=COALESCE(playlist_item_snapshot_json,?)
                   WHERE playlist_item_id=? OR candidate_id IN (SELECT id FROM candidates WHERE playlist_item_id=?)""",
                (snapshot, old_id, old_id),
            )
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            conn.execute(f"DELETE FROM playlist_items WHERE id IN ({placeholders})", stale_ids)  # nosec B608
        conn.execute("UPDATE playlists SET source_name=?,last_synced_at=? WHERE id=?", (source.get("source_name"), utc_now(), playlist_id))
    return {"id": playlist_id, "count": len(items), "message": f"已从来源刷新 {len(items)} 部电影"}

@router.get("/api/playlists")
async def playlists() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.*, COUNT(i.id) AS item_count, SUM(CASE WHEN i.tmdb_id IS NOT NULL THEN 1 ELSE 0 END) AS recognized_count FROM playlists p
               LEFT JOIN playlist_items i ON i.playlist_id=p.id GROUP BY p.id ORDER BY p.position,p.id"""
        ).fetchall()
    return rows_to_dicts(rows)

@router.put("/api/playlists/{playlist_id}/automation")
async def configure_playlist_automation(playlist_id: int, payload: PlaylistAutomationPayload) -> dict[str, Any]:
    if payload.auto_cart:
        raise HTTPException(422, "自动加入下载列表已停用；候选必须由操作者确认")
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
            raise HTTPException(404, "片单不存在")
        conn.execute(
            "UPDATE playlists SET automation_enabled=?,automation_auto_cart=?,automation_batch_size=? WHERE id=?",
            (to_int(payload.enabled), 0, payload.batch_size, playlist_id),
        )
    return {"id": playlist_id, **payload.model_dump(), "auto_cart": False, "auto_download": False}

@router.post("/api/playlists/{playlist_id}/automation/run")
async def run_playlist_automation_now(playlist_id: int) -> dict[str, Any]:
    return await start_playlist_automation(playlist_id)


@router.put("/api/playlists/{playlist_id}/sync-settings")
async def configure_playlist_sync(playlist_id: int, payload: PlaylistSyncPayload) -> dict[str, Any]:
    with connect() as conn:
        playlist = conn.execute("SELECT source_url FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if not playlist:
            raise HTTPException(404, "片单不存在")
        if payload.enabled and not playlist["source_url"]:
            raise HTTPException(422, "只有网址导入的片单可以启用定时同步")
        next_sync = (datetime.now(timezone.utc) + timedelta(hours=payload.interval_hours)).isoformat() if payload.enabled else None
        conn.execute(
            "UPDATE playlists SET sync_enabled=?,sync_interval_hours=?,next_sync_at=? WHERE id=?",
            (to_int(payload.enabled), payload.interval_hours, next_sync, playlist_id),
        )
    return {"id": playlist_id, **payload.model_dump(), "next_sync_at": next_sync}

@router.post("/api/playlists/{playlist_id}/sync-now")
async def sync_playlist_now(playlist_id: int) -> dict[str, Any]:
    return await sync_playlist_incremental(playlist_id)


@router.post("/api/playlists/{playlist_id}/library-scan")
async def scan_playlist_library(playlist_id: int) -> dict[str, Any]:
    if not settings.emby_base_url or not settings.emby_api_key:
        raise HTTPException(422, "请先在设置中配置 Emby 地址与 API Key")
    capacity_rejection = enforce_background_task_capacity(running_library_tasks, MAX_RUNNING_LIBRARY_TASKS, "Emby 状态刷新")
    if capacity_rejection is not None:
        raise HTTPException(429, capacity_rejection)
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
                raise HTTPException(404, "片单不存在")
            active = conn.execute(
                "SELECT id,status FROM library_scan_tasks WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (playlist_id,),
            ).fetchone()
            if active:
                return {"id": active["id"], "status": active["status"], "message": "Emby 状态刷新正在进行"}
            total = conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (playlist_id,)).fetchone()[0]
            task_cursor = conn.execute(
                """INSERT INTO library_scan_tasks(playlist_id,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?)""", (playlist_id, "queued", total, utc_now(), utc_now()),
            )
            task_id = task_cursor.lastrowid
            if task_id is None:
                raise RuntimeError("Emby 状态刷新任务写入失败")
    except sqlite3.IntegrityError as _integrity:
        with connect() as conn:
            active = conn.execute(
                "SELECT id,status FROM library_scan_tasks WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (playlist_id,),
            ).fetchone()
        if not active:
            raise
        return {"id": active["id"], "status": active["status"], "message": "Emby 状态刷新正在进行"}
    running_library_tasks[task_id] = asyncio.create_task(run_library_scan(task_id))
    return {"id": task_id, "message": f"开始刷新 {total} 部影片的 Emby 状态"}


@router.put("/api/playlists/{playlist_id}")
async def update_playlist(playlist_id: int, payload: PlaylistUpdatePayload) -> dict[str, Any]:
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
            raise HTTPException(404, "片单不存在")
        conn.execute("UPDATE playlists SET name=? WHERE id=?", (payload.name.strip(), playlist_id))
    return {"id": playlist_id, "name": payload.name.strip()}

@router.post("/api/playlists/reorder")
async def reorder_playlists(payload: PlaylistOrderPayload) -> dict[str, Any]:
    with connect() as conn:
        existing = {row[0] for row in conn.execute("SELECT id FROM playlists")}
        if len(payload.ids) != len(existing) or len(set(payload.ids)) != len(payload.ids) or set(payload.ids) != existing:
            raise HTTPException(422, "排序列表必须包含全部片单")
        conn.executemany("UPDATE playlists SET position=? WHERE id=?", [(index, site_id) for index, site_id in enumerate(payload.ids, start=1)])
    return {"ids": payload.ids}

@router.post("/api/playlists/{playlist_id}/recognize")
async def recognize_playlist(playlist_id: int, mode: Literal["missing", "verify"] = "missing") -> dict[str, Any]:
    """missing：识别还没识别的影片；verify：补齐来源编号并按 IMDb 校准整份片单。"""
    capacity_rejection = enforce_background_task_capacity(running_recognition_tasks, MAX_RUNNING_RECOGNITION_TASKS, "识别")
    if capacity_rejection is not None:
        raise HTTPException(429, capacity_rejection)
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
                raise HTTPException(404, "片单不存在")
            total = conn.execute(
                """SELECT COUNT(*) FROM playlist_items
                   WHERE playlist_id=? AND (? OR tmdb_id IS NULL OR tmdb_title IS NULL OR tmdb_original_title IS NULL)""",
                (playlist_id, int(mode == "verify")),
            ).fetchone()[0]
            if not total:
                return {"id": None, "status": "completed", "total": 0, "message": "片单已全部识别"}
            active = conn.execute(
                "SELECT id,status,total FROM recognition_tasks WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (playlist_id,),
            ).fetchone()
            if active:
                return {"id": active["id"], "status": active["status"], "total": active["total"]}
            task_cursor = conn.execute(
                "INSERT INTO recognition_tasks(playlist_id,status,total,created_at,updated_at,mode) VALUES(?,?,?,?,?,?)",
                (playlist_id, "queued", total, utc_now(), utc_now(), mode),
            )
            task_id = task_cursor.lastrowid
            if task_id is None:
                raise RuntimeError("识别任务写入失败")
    except sqlite3.IntegrityError as _integrity:
        with connect() as conn:
            active = conn.execute(
                "SELECT id,status,total FROM recognition_tasks WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (playlist_id,),
            ).fetchone()
        if not active:
            raise
        return {"id": active["id"], "status": active["status"], "total": active["total"]}
    running_recognition_tasks[task_id] = asyncio.create_task(run_recognition(task_id))
    return {"id": task_id, "status": "queued", "total": total}


@router.delete("/api/playlists/{playlist_id}")
async def delete_playlist(playlist_id: int) -> dict[str, Any]:
    tasks_to_cancel: list[asyncio.Task[None]] = []
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "片单不存在")
        for table, registry in (
            ("search_tasks", running_tasks),
            ("recognition_tasks", running_recognition_tasks),
            ("library_scan_tasks", running_library_tasks),
            ("automation_runs", running_automation_tasks),
        ):
            ids = conn.execute(
                f"SELECT id FROM {table} WHERE playlist_id=? AND status IN ('queued','running')", (playlist_id,),  # nosec B608
            ).fetchall()
            for row in ids:
                task = registry.get(to_int(row["id"]))
                if task and not task.done():
                    task.cancel()
                    tasks_to_cancel.append(task)
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    with connect() as conn:
        history_items = conn.execute(
            "SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY rank_no", (playlist_id,)
        ).fetchall()
        for item in history_items:
            snapshot = json_value(playlist_item_snapshot(dict(item)))
            conn.execute(
                """UPDATE download_history SET playlist_item_snapshot_json=COALESCE(playlist_item_snapshot_json,?)
                   WHERE playlist_item_id=? OR candidate_id IN (SELECT id FROM candidates WHERE playlist_item_id=?)""",
                (snapshot, item["id"], item["id"]),
            )
        conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    return {"deleted": playlist_id}
