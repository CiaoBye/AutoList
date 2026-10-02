"""AutoList HTTP routes."""

from __future__ import annotations

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
from ..queries import playlists as queries
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
from ..state import fetch_once, poster_cache, remember_poster
from ..tasks import LIBRARY, RECOGNITION, active_playlist_task, cancel_playlist_tasks
from ..util import raster_image_media_type, to_int
from ..responses import AutomationStarted, ImportPreview, PlaylistImported, PlaylistRow, PlaylistSynced

router = APIRouter()


@router.get("/api/playlist-items/{playlist_item_id}/poster")
async def playlist_item_poster(playlist_item_id: int, tag: str = "") -> Response:
    with connect() as conn:
        row = queries.emby_poster_ref(conn, playlist_item_id)
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
            try:
                content, _ = await fetch_once(cache_key, lambda: EmbyClient().poster(emby_item_id))
            except httpx.HTTPStatusError as exc:
                status = 404 if exc.response.status_code == 404 else 502
                raise HTTPException(status, "Emby 海报读取失败") from exc
            except Exception as exc:
                raise HTTPException(502, f"Emby 海报读取失败：{safe_error(exc)}") from exc
            media_type = raster_image_media_type(content) or ""
            if not media_type or len(content) > 8 * 1024 * 1024:
                raise HTTPException(422, "Emby 返回的海报格式无效")
        except HTTPException:
            # Emby 里只有 .strm 占位的影片有图片标记，取图却返回 500：改用 TMDB 海报，缓存时间较短。
            if not settings.tmdb_api_key:
                raise
            from .images import FALLBACK_POSTER_CACHE, playlist_item_tmdb_poster

            response = await playlist_item_tmdb_poster(playlist_item_id)
            response.headers["Cache-Control"] = FALLBACK_POSTER_CACHE
            return response
        remember_poster(cache_key, (content, media_type))
    return Response(
        content=content, media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )

@router.post("/api/playlists/import/preview", response_model=ImportPreview)
async def preview_playlist_import(payload: ImportPayload) -> dict[str, Any]:
    try:
        source_name, items, source = await resolve_import(payload)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(422, f"片单获取失败：{safe_error(exc)}") from exc
    if not items:
        raise HTTPException(422, "没有获取到可导入的电影")
    return {"name": payload.name or source_name, "count": len(items), "source_type": source.get("source_type"), "sample": items[:8]}

@router.post("/api/playlists/import", response_model=PlaylistImported)
async def import_playlist(payload: ImportPayload) -> dict[str, Any]:
    try:
        source_name, items, source = await resolve_import(payload)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(422, f"片单获取失败：{safe_error(exc)}") from exc
    if not items:
        raise HTTPException(422, "没有可导入的影片")
    name = payload.name or source_name or f"片单 {datetime.now().strftime('%Y-%m-%d')}"
    with connect() as conn:
        playlist_id = queries.insert_playlist(conn, name, source)
        queries.insert_imported_items(conn, playlist_id, items)
        # 导入后自动识别只是便利步骤：未配置 TMDB 或识别容量已满时跳过，并把原因返回给界面。
        task_id = None
        recognition_note = None
        if not settings.tmdb_api_key:
            recognition_note = "未配置 TMDB API Key，已跳过自动识别"
        else:
            recognition_note = RECOGNITION.capacity_error()
            if recognition_note is None:
                task_id = queries.insert_recognition_task(conn, playlist_id, len(items))
    if task_id is not None:
        RECOGNITION.start(task_id, run_recognition(task_id))
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
        playlist = queries.get_playlist(conn, playlist_id)
        if not playlist:
            raise HTTPException(404, "片单不存在")
        if not playlist["source_url"]:
            raise HTTPException(422, "该片单不是通过网址导入的")
        if active := active_playlist_task(conn, playlist_id):
            raise HTTPException(409, f"片单仍有{active.label}任务运行，请完成后再刷新")
    try:
        source = await PlaylistSourceFetcher().fetch(str(playlist["source_url"]), 10000)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(422, f"片单刷新失败：{safe_error(exc)}") from exc
    items = normalize_import_items(source.get("items", []))
    if not items:
        raise HTTPException(422, "来源没有返回可用电影，已保留现有片单")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not queries.playlist_exists(conn, playlist_id):
            raise HTTPException(404, "片单不存在")
        if active := active_playlist_task(conn, playlist_id):
            raise HTTPException(409, f"片单刷新期间启动了{active.label}任务，已保留现有片单")
        existing_rows = queries.playlist_items_by_rank(conn, playlist_id)

        indexed: dict[tuple[str, str], list[Any]] = {}
        for old in existing_rows:
            for key in item_identity_keys(dict(old)):
                indexed.setdefault(key, []).append(old)
        used_ids: set[int] = set()
        matched_ids: set[int] = set()
        rank_offset = max([to_int(row["rank_no"]) for row in existing_rows] + [len(items), 1]) + len(existing_rows) + 1
        queries.shift_ranks(conn, playlist_id, rank_offset)
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
                queries.update_item_from_source(
                    conn, old_id, item, imdb_id=item.get("imdb_id") or old["imdb_id"],
                    source_tmdb_id=source_tmdb_id, source_ref=source_ref,
                    reset_identity=incoming_tmdb_id is not None and old["tmdb_id"] not in (None, incoming_tmdb_id),
                )
            else:
                matched_ids.add(queries.insert_source_item(conn, playlist_id, item))
        stale_ids = [to_int(row["id"]) for row in existing_rows if to_int(row["id"]) not in matched_ids]
        for old in existing_rows:
            old_id = to_int(old["id"])
            if old_id in matched_ids:
                continue
            queries.keep_history_snapshot(conn, old_id, json_value(playlist_item_snapshot(dict(old))))
        queries.delete_items(conn, stale_ids)
        queries.mark_playlist_synced(conn, playlist_id, source.get("source_name"))
    return {"id": playlist_id, "count": len(items), "message": f"已从来源刷新 {len(items)} 部电影"}

@router.get("/api/playlists", response_model=list[PlaylistRow])
async def playlists() -> list[dict[str, Any]]:
    with connect() as conn:
        return queries.playlists_with_counts(conn)

@router.put("/api/playlists/{playlist_id}/automation")
async def configure_playlist_automation(playlist_id: int, payload: PlaylistAutomationPayload) -> dict[str, Any]:
    if payload.auto_select:
        raise HTTPException(422, "自动加入待入馆清单已停用；候选必须由操作者确认")
    with connect() as conn:
        if not queries.playlist_exists(conn, playlist_id):
            raise HTTPException(404, "片单不存在")
        queries.set_playlist_automation(conn, playlist_id, enabled=payload.enabled, batch_size=payload.batch_size)
    return {"id": playlist_id, **payload.model_dump(), "auto_select": False, "auto_download": False}

@router.post("/api/playlists/{playlist_id}/automation/run", response_model=AutomationStarted)
async def run_playlist_automation_now(playlist_id: int) -> dict[str, Any]:
    return await start_playlist_automation(playlist_id)


@router.put("/api/playlists/{playlist_id}/sync-settings")
async def configure_playlist_sync(playlist_id: int, payload: PlaylistSyncPayload) -> dict[str, Any]:
    with connect() as conn:
        playlist = queries.get_playlist(conn, playlist_id)
        if not playlist:
            raise HTTPException(404, "片单不存在")
        if payload.enabled and not playlist["source_url"]:
            raise HTTPException(422, "只有网址导入的片单可以启用定时同步")
        next_sync = (datetime.now(timezone.utc) + timedelta(hours=payload.interval_hours)).isoformat() if payload.enabled else None
        queries.set_playlist_sync(conn, playlist_id, enabled=payload.enabled, interval_hours=payload.interval_hours, next_sync_at=next_sync)
    return {"id": playlist_id, **payload.model_dump(), "next_sync_at": next_sync}

@router.post("/api/playlists/{playlist_id}/sync-now", response_model=PlaylistSynced)
async def sync_playlist_now(playlist_id: int) -> dict[str, Any]:
    return await sync_playlist_incremental(playlist_id)


@router.post("/api/playlists/{playlist_id}/library-scan")
async def scan_playlist_library(playlist_id: int) -> dict[str, Any]:
    if not settings.emby_base_url or not settings.emby_api_key:
        raise HTTPException(422, "请先在设置中配置 Emby 地址与 API Key")
    capacity_rejection = LIBRARY.capacity_error()
    if capacity_rejection is not None:
        raise HTTPException(429, capacity_rejection)
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not queries.playlist_exists(conn, playlist_id):
                raise HTTPException(404, "片单不存在")
            active = queries.active_library_scan(conn, playlist_id)
            if active:
                return {"id": active["id"], "status": active["status"], "message": "Emby 状态刷新正在进行"}
            total = queries.count_playlist_items(conn, playlist_id)
            task_id = queries.insert_library_scan_task(conn, playlist_id, total)
    except sqlite3.IntegrityError as _integrity:
        with connect() as conn:
            active = queries.active_library_scan(conn, playlist_id)
        if not active:
            raise
        return {"id": active["id"], "status": active["status"], "message": "Emby 状态刷新正在进行"}
    LIBRARY.start(task_id, run_library_scan(task_id))
    return {"id": task_id, "message": f"开始刷新 {total} 部影片的 Emby 状态"}


@router.put("/api/playlists/{playlist_id}")
async def update_playlist(playlist_id: int, payload: PlaylistUpdatePayload) -> dict[str, Any]:
    with connect() as conn:
        if not queries.playlist_exists(conn, playlist_id):
            raise HTTPException(404, "片单不存在")
        queries.rename_playlist(conn, playlist_id, payload.name.strip())
    return {"id": playlist_id, "name": payload.name.strip()}

@router.post("/api/playlists/reorder")
async def reorder_playlists(payload: PlaylistOrderPayload) -> dict[str, Any]:
    with connect() as conn:
        existing = queries.playlist_ids(conn)
        if len(payload.ids) != len(existing) or len(set(payload.ids)) != len(payload.ids) or set(payload.ids) != existing:
            raise HTTPException(422, "排序列表必须包含全部片单")
        queries.set_playlist_positions(conn, payload.ids)
    return {"ids": payload.ids}

@router.post("/api/playlists/{playlist_id}/recognize")
async def recognize_playlist(playlist_id: int, mode: Literal["missing", "verify"] = "missing") -> dict[str, Any]:
    """missing：识别还没识别的影片；verify：补齐来源编号并按 IMDb 校准整份片单。"""
    capacity_rejection = RECOGNITION.capacity_error()
    if capacity_rejection is not None:
        raise HTTPException(429, capacity_rejection)
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not queries.playlist_exists(conn, playlist_id):
                raise HTTPException(404, "片单不存在")
            total = queries.count_playlist_items(conn, playlist_id, unrecognized_only=mode == "missing")
            if not total:
                return {"id": None, "status": "completed", "total": 0, "message": "片单已全部识别"}
            active = queries.active_recognition_task(conn, playlist_id)
            if active:
                return {"id": active["id"], "status": active["status"], "total": active["total"]}
            task_id = queries.insert_recognition_task(conn, playlist_id, total, mode)
    except sqlite3.IntegrityError as _integrity:
        with connect() as conn:
            active = queries.active_recognition_task(conn, playlist_id)
        if not active:
            raise
        return {"id": active["id"], "status": active["status"], "total": active["total"]}
    RECOGNITION.start(task_id, run_recognition(task_id))
    return {"id": task_id, "status": "queued", "total": total}


@router.delete("/api/playlists/{playlist_id}")
async def delete_playlist(playlist_id: int) -> dict[str, Any]:
    with connect() as conn:
        if not queries.playlist_exists(conn, playlist_id):
            raise HTTPException(404, "片单不存在")
        await cancel_playlist_tasks(conn, playlist_id)
    with connect() as conn:
        for item in queries.playlist_items_by_rank(conn, playlist_id):
            queries.keep_history_snapshot(conn, to_int(item["id"]), json_value(playlist_item_snapshot(dict(item))))
        queries.delete_playlist(conn, playlist_id)
    return {"deleted": playlist_id}
