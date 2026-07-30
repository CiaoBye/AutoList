"""Playlist automation, incremental sync, and background scheduler."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from ..clients import EmbyClient
from ..database import cleanup_old_data, connect, json_value
from ..domain.titles import canonical_item_title, canonical_item_year
from ..list_sources import PlaylistSourceFetcher
from ..security import safe_error, sanitize_sensitive_text
from ..state import running_automation_tasks, running_recognition_tasks, running_tasks, scheduler_task
from ..util import rows_to_dicts, utc_now
from .library import library_details
from .recognition import persist_tmdb_item, recognize_movie
from .imports import normalize_import_items
from .search import begin_search_task_slot, run_search, searchable_playlist_items
from .sites import refresh_stale_site_account_stats


def update_recognition_task(task_id: int, **values: Any) -> None:
    if not set(values).issubset({"status", "completed", "matched", "error_message"}):
        raise ValueError("无效的识别任务字段")
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in values)
    with connect() as conn:
        # Dynamic column names are restricted by the allowlist above.
        conn.execute(f"UPDATE recognition_tasks SET {assignments} WHERE id=?", (*values.values(), task_id))  # nosec B608


async def run_recognition(task_id: int) -> None:
    with connect() as conn:
        task = conn.execute("SELECT * FROM recognition_tasks WHERE id=?", (task_id,)).fetchone()
        items = conn.execute(
            """SELECT * FROM playlist_items
               WHERE playlist_id=? AND (tmdb_id IS NULL OR tmdb_title IS NULL OR tmdb_original_title IS NULL)
               ORDER BY rank_no""", (task["playlist_id"],),
        ).fetchall()
    update_recognition_task(task_id, status="running")
    matched, errors = 0, []
    try:
        for completed, item in enumerate(items, start=1):
            try:
                media = await recognize_movie(item["original_title"], item["year"], item["imdb_id"])
                if media:
                    persist_tmdb_item(int(item["id"]), media, item["imdb_id"])
                    matched += 1
                else:
                    errors.append(f"#{item['rank_no']} 未识别")
            except Exception as exc:
                errors.append(f"#{item['rank_no']} {safe_error(exc)}")
            update_recognition_task(task_id, completed=completed, matched=matched)
        update_recognition_task(
            task_id, status="partial" if errors else "completed", completed=len(items), matched=matched,
            error_message="；".join(errors[:8])[:500] if errors else None,
        )
    except asyncio.CancelledError:
        update_recognition_task(task_id, status="cancelled")
        raise
    except Exception as exc:
        update_recognition_task(task_id, status="failed", error_message=safe_error(exc))
    finally:
        running_recognition_tasks.pop(task_id, None)


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
        items = queue["items"]
        update_automation_run(run_id, status="running", stage="recognition", total=len(items))
        emby = EmbyClient()
        recognized = searched = recommended = 0
        searchable_ids: list[int] = []
        for completed, item in enumerate(items, start=1):
            tmdb_id = item["tmdb_id"]
            if not tmdb_id or not item["tmdb_title"] or not item["tmdb_original_title"]:
                media = await recognize_movie(item["original_title"], item["year"], item["imdb_id"])
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
                task_id = int(conn.execute(
                    """INSERT INTO search_tasks(
                         playlist_id,range_start,range_end,status,total,trigger,site_ids_json,item_ids_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (playlist["id"], ranks[0], ranks[1], "queued", len(searchable_ids), "automation",
                     json_value(site_ids), json_value(searchable_ids), now, now),
                ).lastrowid)
            update_automation_run(run_id, stage="search")
            running_tasks[task_id] = asyncio.current_task()  # visible in health while the nested search runs
            await run_search(task_id)
            searched = len(searchable_ids)
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
        message = f"处理 {len(items)} 部，新增识别 {recognized}，搜索 {searched}，推荐 {recommended}；候选需人工确认"
        update_automation_run(
            run_id, status="completed", stage="completed", completed=len(items), recognized=recognized,
            searched=searched, recommended=recommended, message=message,
        )
        add_notification("新增影片处理完成", message, "success")
    except asyncio.CancelledError:
        update_automation_run(run_id, status="cancelled", message="新片处理任务已取消")
        raise
    except Exception as exc:
        reason = safe_error(exc)
        update_automation_run(run_id, status="failed", message=reason)
        add_notification("新增影片处理失败", reason, "error")
    finally:
        running_automation_tasks.pop(run_id, None)


async def sync_playlist_incremental(playlist_id: int, trigger: str = "manual") -> dict[str, Any]:
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
                "SELECT imdb_id,tmdb_id,original_title,year FROM playlist_items WHERE playlist_id=?", (playlist_id,),
            ).fetchall()
            keys = {
                ("imdb", str(row["imdb_id"])) if row["imdb_id"] else
                ("tmdb", str(row["tmdb_id"])) if row["tmdb_id"] else
                ("title", re.sub(r"\W+", "", str(row["original_title"]).lower()), str(row["year"] or ""))
                for row in existing
            }
            max_rank = int(conn.execute("SELECT COALESCE(MAX(rank_no),0) FROM playlist_items WHERE playlist_id=?", (playlist_id,)).fetchone()[0])
            additions = []
            for item in incoming:
                key = (("imdb", str(item["imdb_id"])) if item.get("imdb_id") else
                       ("tmdb", str(item["tmdb_id"])) if item.get("tmdb_id") else
                       ("title", re.sub(r"\W+", "", str(item["original_title"]).lower()), str(item.get("year") or "")))
                if key in keys:
                    continue
                keys.add(key)
                max_rank += 1
                additions.append({**item, "playlist_id": playlist_id, "rank_no": max_rank})
            if additions:
                conn.executemany(
                    """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,tmdb_id)
                       VALUES(:playlist_id,:rank_no,:imdb_id,:original_title,:year,:chinese_title,:tmdb_id)""", additions,
                )
            next_sync = (datetime.now(timezone.utc) + timedelta(hours=int(playlist["sync_interval_hours"] or 24))).isoformat()
            message = f"增量同步完成，新增 {len(additions)} 部，保留现有 {len(existing)} 部"
            conn.execute(
                """UPDATE playlists SET source_name=?,last_synced_at=?,next_sync_at=?,last_sync_status='completed',last_sync_message=?
                   WHERE id=?""", (source.get("source_name"), utc_now(), next_sync, message, playlist_id),
            )
        add_notification("片单来源已同步", f"{playlist['name']}：{message}", "success")
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
        raise


async def start_playlist_automation(playlist_id: int, trigger: str = "manual") -> dict[str, Any]:
    with connect() as conn:
        playlist = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if not playlist:
            raise HTTPException(404, "片单不存在")
        active = conn.execute(
            "SELECT id FROM automation_runs WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (playlist_id,),
        ).fetchone()
        if active:
            return {"id": int(active["id"]), "status": "running", "message": "新增影片处理任务正在运行"}
        now = utc_now()
        run_id = int(conn.execute(
            "INSERT INTO automation_runs(playlist_id,trigger,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (playlist_id, trigger, "queued", "queued", now, now),
        ).lastrowid)
    running_automation_tasks[run_id] = asyncio.create_task(run_playlist_automation(run_id))
    return {"id": run_id, "status": "queued", "message": "已开始识别并搜索未入库影片；不会自动下载"}


async def sync_scheduler() -> None:
    while True:
        await asyncio.sleep(60)
        cleanup_old_data()
        await refresh_stale_site_account_stats()
        now = utc_now()
        with connect() as conn:
            due = [int(row["id"]) for row in conn.execute(
                """SELECT id FROM playlists WHERE sync_enabled=1 AND source_url IS NOT NULL AND source_url!=''
                   AND (next_sync_at IS NULL OR next_sync_at<=?)""", (now,),
            ).fetchall()]
        for playlist_id in due:
            try:
                await sync_playlist_incremental(playlist_id, "schedule")
            except Exception:
                continue
