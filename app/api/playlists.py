"""AutoList HTTP routes."""

from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..clients import EmbyClient
from ..config import settings
from ..database import connect, json_value
from ..list_sources import PlaylistSourceFetcher
from ..schemas import (
    ImportPayload,
    PlaylistAutomationPayload,
    PlaylistOrderPayload,
    PlaylistSyncPayload,
    PlaylistUpdatePayload,
)
from ..security import safe_error
from ..services.automation import run_recognition, start_playlist_automation, sync_playlist_incremental
from ..services.history import playlist_item_snapshot
from ..services.imports import normalize_import_items, resolve_import
from ..services.library import library_details, run_library_scan
from ..services.search import searchable_playlist_items
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
from ..util import raster_image_media_type, resource_fingerprint, rows_to_dicts, utc_now

router = APIRouter()
MAX_LEGACY_PLAYLIST_ITEMS_RESPONSE = 5000

async def hydrate_recent_emby_posters(items: list[dict[str, Any]]) -> None:
    """Backfill legacy Emby references for the small home-page shelf without a full rescan."""
    if not items or not settings.emby_base_url or not settings.emby_api_key:
        return
    emby = EmbyClient()
    semaphore = asyncio.Semaphore(3)

    async def hydrate(item: dict[str, Any]) -> None:
        if item.get("emby_item_id") or item.get("library_state") not in {"in_library", "strm"}:
            return
        async with semaphore:
            _, emby_item_id, image_tag = await library_details(
                emby, item.get("tmdb_title") or item.get("chinese_title") or item["original_title"],
                item.get("tmdb_year") or item.get("year"), item.get("tmdb_id"),
                item.get("tmdb_imdb_id") or item.get("imdb_id"),
            )
        if not emby_item_id:
            return
        item["emby_item_id"] = emby_item_id
        item["emby_image_tag"] = image_tag
        with connect() as conn:
            conn.execute(
                "UPDATE playlist_items SET emby_item_id=?,emby_image_tag=? WHERE id=?",
                (emby_item_id, image_tag, item["id"]),
            )

    await asyncio.gather(*(hydrate(item) for item in items))

@router.get("/api/overview")
async def overview() -> dict[str, Any]:
    with connect() as conn:
        playlist = conn.execute(
            """SELECT p.id, p.name, COUNT(i.id) AS item_count FROM playlists p
               LEFT JOIN playlist_items i ON i.playlist_id=p.id GROUP BY p.id ORDER BY p.position,p.id LIMIT 1"""
        ).fetchone()
        playlist_stats = conn.execute(
            """SELECT
                 SUM(CASE WHEN tmdb_id IS NOT NULL THEN 1 ELSE 0 END) AS recognized_count,
                 SUM(CASE WHEN library_state='in_library' THEN 1 ELSE 0 END) AS in_library_count
               FROM playlist_items WHERE playlist_id=?""",
            (playlist["id"],),
        ).fetchone() if playlist else None
        if playlist and settings.dashboard_random_posters:
            daily_seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
            recent_items = rows_to_dicts(conn.execute(
                """SELECT id,rank_no,imdb_id,tmdb_id,original_title,chinese_title,year,
                          tmdb_title,tmdb_original_title,tmdb_year,tmdb_imdb_id,
                          library_state,emby_item_id,emby_image_tag
                   FROM playlist_items WHERE playlist_id=? AND library_state='in_library'
                   ORDER BY ((id * (1103515245 + (? % 997))) & 2147483647),rank_no LIMIT 6""",
                (playlist["id"], daily_seed),
            ).fetchall())
        else:
            recent_items = rows_to_dicts(conn.execute(
                """SELECT id,rank_no,imdb_id,tmdb_id,original_title,chinese_title,year,
                          tmdb_title,tmdb_original_title,tmdb_year,tmdb_imdb_id,
                          library_state,emby_item_id,emby_image_tag
                   FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 6""",
                (playlist["id"],),
            ).fetchall()) if playlist else []
        latest_task = conn.execute(
            "SELECT * FROM search_tasks WHERE status!='archived' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_candidates = conn.execute(
            "SELECT playlist_item_id,title,size,resource_key FROM candidates WHERE task_id=? AND eligibility='eligible'", (latest_task["id"],),
        ).fetchall() if latest_task else []
        latest_candidate_count = len({
            (row["playlist_item_id"], row["resource_key"] or resource_fingerprint(row["title"], row["size"]))
            for row in latest_candidates
        })
        cart_count = conn.execute("SELECT COUNT(*) FROM cart_items").fetchone()[0]
        history_count = conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0]
    await hydrate_recent_emby_posters(recent_items)
    for item in recent_items:
        item["poster_url"] = (
            f"/api/playlist-items/{item['id']}/poster?tag={item['emby_image_tag']}"
            if item.get("emby_item_id") and item.get("emby_image_tag") else None
        )
    return {
        "playlist_id": playlist["id"] if playlist else None,
        "playlist_name": playlist["name"] if playlist else None,
        "item_count": playlist["item_count"] if playlist else 0,
        "recognized_count": int(playlist_stats["recognized_count"] or 0) if playlist_stats else 0,
        "in_library_count": int(playlist_stats["in_library_count"] or 0) if playlist_stats else 0,
        "pending_count": max(0, int(playlist["item_count"] or 0) - int(playlist_stats["in_library_count"] or 0)) if playlist and playlist_stats else 0,
        "recent_items": recent_items,
        "cart_count": cart_count,
        "history_count": history_count,
        "latest_candidate_count": latest_candidate_count,
        "latest_task": dict(latest_task) if latest_task else None,
    }

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
        conn.executemany(
            """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,tmdb_id)
               VALUES(:playlist_id,:rank_no,:imdb_id,:original_title,:year,:chinese_title,:tmdb_id)""",
            [{"playlist_id": playlist_id, "tmdb_id": None, **item} for item in items],
        )
    return {"id": playlist_id, "name": name, "count": len(items)}

@router.post("/api/playlists/{playlist_id}/refresh-source")
async def refresh_playlist_source(playlist_id: int) -> dict[str, Any]:
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

        def identity_keys(value: Any) -> list[tuple[str, str]]:
            keys: list[tuple[str, str]] = []
            if value.get("imdb_id"):
                keys.append(("imdb", str(value["imdb_id"]).casefold()))
            if value.get("tmdb_id"):
                keys.append(("tmdb", str(value["tmdb_id"])))
            normalized_title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value.get("original_title") or "").casefold())
            if normalized_title:
                keys.append(("title", f"{normalized_title}:{value.get('year') or ''}"))
            return keys

        indexed: dict[tuple[str, str], list[Any]] = {}
        for old in existing_rows:
            for key in identity_keys(dict(old)):
                indexed.setdefault(key, []).append(old)
        used_ids: set[int] = set()
        matched_ids: set[int] = set()
        rank_offset = max([int(row["rank_no"] or 0) for row in existing_rows] + [len(items), 1]) + len(existing_rows) + 1
        conn.execute("UPDATE playlist_items SET rank_no=rank_no+? WHERE playlist_id=?", (rank_offset, playlist_id))
        for item in items:
            matches = [
                old for key in identity_keys(item)
                for old in indexed.get(key, [])
                if int(old["id"]) not in used_ids
            ]
            old = matches[0] if matches else None
            if old:
                old_id = int(old["id"])
                used_ids.add(old_id)
                matched_ids.add(old_id)
                incoming_tmdb_id = item.get("tmdb_id")
                tmdb_changed = incoming_tmdb_id is not None and old["tmdb_id"] not in (None, incoming_tmdb_id)
                if tmdb_changed:
                    conn.execute(
                        """UPDATE playlist_items SET rank_no=?,imdb_id=?,original_title=?,year=?,chinese_title=?,tmdb_id=?,
                                  tmdb_title=NULL,tmdb_original_title=NULL,tmdb_year=NULL,tmdb_imdb_id=NULL,tmdb_checked_at=NULL,
                                  library_state='unknown',library_checked_at=NULL,emby_item_id=NULL,emby_image_tag=NULL WHERE id=?""",
                        (item["rank_no"], item.get("imdb_id") or old["imdb_id"], item["original_title"], item.get("year"), item.get("chinese_title"), incoming_tmdb_id, old_id),
                    )
                else:
                    conn.execute(
                        """UPDATE playlist_items SET rank_no=?,imdb_id=?,original_title=?,year=?,chinese_title=?,tmdb_id=? WHERE id=?""",
                        (item["rank_no"], item.get("imdb_id") or old["imdb_id"], item["original_title"], item.get("year"), item.get("chinese_title"), incoming_tmdb_id if incoming_tmdb_id is not None else old["tmdb_id"], old_id),
                    )
            else:
                new_id = conn.execute(
                    """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,tmdb_id)
                       VALUES(?,?,?,?,?,?,?)""",
                    (playlist_id, item["rank_no"], item.get("imdb_id"), item["original_title"], item.get("year"), item.get("chinese_title"), item.get("tmdb_id")),
                ).lastrowid
                matched_ids.add(int(new_id))
        stale_ids = [int(row["id"]) for row in existing_rows if int(row["id"]) not in matched_ids]
        for old in existing_rows:
            old_id = int(old["id"])
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
            (int(payload.enabled), 0, payload.batch_size, playlist_id),
        )
    return {"id": playlist_id, **payload.model_dump(), "auto_cart": False, "auto_download": False}

@router.post("/api/playlists/{playlist_id}/automation/run")
async def run_playlist_automation_now(playlist_id: int) -> dict[str, Any]:
    return await start_playlist_automation(playlist_id)

@router.get("/api/automation-runs")
async def automation_runs(playlist_id: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 100))
    with connect() as conn:
        if playlist_id is None:
            rows = conn.execute("SELECT * FROM automation_runs ORDER BY id DESC LIMIT ?", (safe_limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM automation_runs WHERE playlist_id=? ORDER BY id DESC LIMIT ?", (playlist_id, safe_limit),
            ).fetchall()
    return rows_to_dicts(rows)

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
            (int(payload.enabled), payload.interval_hours, next_sync, playlist_id),
        )
    return {"id": playlist_id, **payload.model_dump(), "next_sync_at": next_sync}

@router.post("/api/playlists/{playlist_id}/sync-now")
async def sync_playlist_now(playlist_id: int) -> dict[str, Any]:
    return await sync_playlist_incremental(playlist_id)

@router.get("/api/notifications")
async def notifications(limit: int = 30, unread_only: bool = False) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 100))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM notifications {'WHERE read=0' if unread_only else ''} ORDER BY id DESC LIMIT ?",  # nosec B608
            (safe_limit,),
        ).fetchall()
    return rows_to_dicts(rows)

@router.post("/api/notifications/read-all")
async def read_all_notifications() -> dict[str, Any]:
    with connect() as conn:
        count = conn.execute("UPDATE notifications SET read=1 WHERE read=0").rowcount
    return {"updated": count}

@router.get("/api/playlists/{playlist_id}/items")
async def playlist_items(
    playlist_id: int,
    page: int | None = None,
    page_size: int = 50,
    query: str = "",
    library_state: str = "all",
) -> Any:
    """Return a backwards-compatible full list or a bounded, filtered page."""
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
            raise HTTPException(404, "片单不存在")
        if page is None:
            # Keep the legacy list-shaped response for existing callers, but
            # never materialize an unbounded playlist into one JSON response.
            rows = conn.execute(
                "SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT ?",
                (playlist_id, MAX_LEGACY_PLAYLIST_ITEMS_RESPONSE),
            ).fetchall()
            return rows_to_dicts(rows)
        safe_page = max(1, page)
        safe_page_size = max(1, min(page_size, 200))
        conditions = ["playlist_id=?"]
        params: list[Any] = [playlist_id]
        normalized_query = query.strip()
        if normalized_query:
            conditions.append("(original_title LIKE ? ESCAPE '\\' OR chinese_title LIKE ? ESCAPE '\\' OR tmdb_title LIKE ? ESCAPE '\\' OR tmdb_original_title LIKE ? ESCAPE '\\' OR imdb_id LIKE ? ESCAPE '\\' OR tmdb_imdb_id LIKE ? ESCAPE '\\')")
            escaped = normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            params.extend((pattern, pattern, pattern, pattern, pattern, pattern))
        if library_state == "not_downloaded":
            conditions.append("library_state!='in_library'")
        elif library_state in {"in_library", "unknown"}:
            conditions.append("library_state=?")
            params.append(library_state)
        elif library_state != "all":
            raise HTTPException(422, "无效的 Emby 状态筛选")
        where = " AND ".join(conditions)
        # SQL fragments in `conditions` are fixed literals; user values remain bound parameters.
        total = int(conn.execute(f"SELECT COUNT(*) FROM playlist_items WHERE {where}", params).fetchone()[0])  # nosec B608
        pages = max(1, math.ceil(total / safe_page_size))
        safe_page = min(safe_page, pages)
        rows = conn.execute(
            f"SELECT * FROM playlist_items WHERE {where} ORDER BY rank_no LIMIT ? OFFSET ?",  # nosec B608
            (*params, safe_page_size, (safe_page - 1) * safe_page_size),
        ).fetchall()
    return {
        "items": rows_to_dicts(rows), "total": total, "page": safe_page,
        "page_size": safe_page_size, "pages": pages,
    }

@router.post("/api/playlists/{playlist_id}/library-scan")
async def scan_playlist_library(playlist_id: int) -> dict[str, Any]:
    enforce_background_task_capacity(running_library_tasks, MAX_RUNNING_LIBRARY_TASKS, "Emby 状态刷新")
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
            raise HTTPException(404, "片单不存在")
        active = conn.execute(
            "SELECT id FROM library_scan_tasks WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (playlist_id,),
        ).fetchone()
        if active:
            return {"id": active["id"], "message": "Emby 状态刷新正在进行"}
        total = conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (playlist_id,)).fetchone()[0]
        task_id = conn.execute(
            """INSERT INTO library_scan_tasks(playlist_id,status,total,created_at,updated_at)
               VALUES(?,?,?,?,?)""", (playlist_id, "queued", total, utc_now(), utc_now()),
        ).lastrowid
    running_library_tasks[task_id] = asyncio.create_task(run_library_scan(task_id))
    return {"id": task_id, "message": f"开始刷新 {total} 部影片的 Emby 状态"}

@router.get("/api/library-scan-tasks/{task_id}")
async def get_library_scan_task(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        task = conn.execute("SELECT * FROM library_scan_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "状态刷新任务不存在")
    return dict(task)

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
async def recognize_playlist(playlist_id: int) -> dict[str, Any]:
    enforce_background_task_capacity(running_recognition_tasks, MAX_RUNNING_RECOGNITION_TASKS, "识别")
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
            raise HTTPException(404, "片单不存在")
        total = conn.execute(
            """SELECT COUNT(*) FROM playlist_items
               WHERE playlist_id=? AND (tmdb_id IS NULL OR tmdb_title IS NULL OR tmdb_original_title IS NULL)""",
            (playlist_id,),
        ).fetchone()[0]
        if not total:
            return {"id": None, "status": "completed", "total": 0, "message": "片单已全部识别"}
        active = conn.execute(
            "SELECT id FROM recognition_tasks WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (playlist_id,),
        ).fetchone()
        if active:
            return {"id": active["id"], "status": "running", "total": total}
        task_id = conn.execute(
            "INSERT INTO recognition_tasks(playlist_id,status,total,created_at,updated_at) VALUES(?,?,?,?,?)",
            (playlist_id, "queued", total, utc_now(), utc_now()),
        ).lastrowid
    running_recognition_tasks[task_id] = asyncio.create_task(run_recognition(task_id))
    return {"id": task_id, "status": "queued", "total": total}

@router.get("/api/recognition-tasks/{task_id}")
async def recognition_task_status(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        task = conn.execute("SELECT * FROM recognition_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "识别任务不存在")
    return dict(task)

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
                task = registry.get(int(row["id"]))
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

@router.get("/api/playlists/{playlist_id}/searchable-items")
async def searchable_items(playlist_id: int, limit: int = 2000) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 10000))
    return await searchable_playlist_items(playlist_id, safe_limit)
