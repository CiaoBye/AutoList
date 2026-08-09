"""AutoList HTTP routes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException

from ..database import connect, json_value
from ..schemas import TaskPayload
from ..services.search import (
    MAX_SEARCH_ITEMS,
    begin_search_task_slot,
    create_followup_search_task,
    run_search,
    searchable_playlist_items,
    update_task,
)
from ..state import prune_raw_candidates, raw_candidates, running_tasks
from ..util import resource_fingerprint, rows_to_dicts, safe_detail_url, utc_now, volume_factor_value

router = APIRouter()
MAX_CANDIDATE_RESPONSE = 5000

@router.post("/api/search-tasks")
async def create_task(payload: TaskPayload) -> dict[str, Any]:
    if payload.scope == "range" and payload.range_end < payload.range_start:
        raise HTTPException(422, "结束序号不能小于起始序号")
    item_ids: list[int] = []
    if payload.scope == "pending":
        queue = await searchable_playlist_items(payload.playlist_id, payload.count)
        if queue.get("download_state") == "unknown":
            raise HTTPException(503, "无法确认 Transmission 当前下载任务，已暂停创建搜索任务")
        item_ids = [int(item["id"]) for item in queue["items"]]
        if not item_ids:
            raise HTTPException(422, "当前片单没有可搜索的未入库影片")
        range_start = min(int(item["rank_no"]) for item in queue["items"])
        range_end = max(int(item["rank_no"]) for item in queue["items"])
    else:
        range_start, range_end = payload.range_start, payload.range_end
    with connect() as conn:
        begin_search_task_slot(conn)
        selected_items = conn.execute(
            "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no BETWEEN ? AND ? ORDER BY rank_no",
            (payload.playlist_id, range_start, range_end),
        ).fetchall()
        total = len(selected_items)
        if not total:
            raise HTTPException(422, "所选范围没有影片")
        if total > MAX_SEARCH_ITEMS:
            raise HTTPException(413, f"单次搜索最多处理 {MAX_SEARCH_ITEMS} 部影片")
        site_ids = [
            int(row["id"]) for row in conn.execute(
                "SELECT id FROM pt_sites WHERE enabled=1 AND search_enabled=1 ORDER BY priority,id",
            ).fetchall()
        ]
        if not site_ids:
            raise HTTPException(422, "请先在站点配置中选择至少一个参与搜索的站点")
        if not item_ids:
            item_ids = [int(row["id"]) for row in selected_items]
        task_id = conn.execute(
            """INSERT INTO search_tasks(
                 playlist_id,range_start,range_end,status,total,trigger,site_ids_json,item_ids_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (payload.playlist_id, range_start, range_end, "queued", len(item_ids),
             "pending" if payload.scope == "pending" else "manual", json_value(site_ids), json_value(item_ids),
             utc_now(), utc_now()),
        ).lastrowid
    running_tasks[task_id] = asyncio.create_task(run_search(task_id))
    return {"id": task_id, "status": "queued", "total": len(item_ids) or total, "scope": payload.scope}

@router.get("/api/search-tasks")
async def search_tasks(playlist_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 100))
    with connect() as conn:
        if playlist_id is None:
            rows = conn.execute(
                "SELECT * FROM search_tasks WHERE status!='archived' ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM search_tasks WHERE playlist_id=? AND status!='archived' ORDER BY id DESC LIMIT ?",
                (playlist_id, safe_limit),
            ).fetchall()
        result = rows_to_dicts(rows)
        for task in result:
            summary = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
                   FROM search_attempts WHERE task_id=?""", (task["id"],),
            ).fetchone()
            task["attempt_summary"] = dict(summary)
    return result

@router.post("/api/search-tasks/{task_id}/cancel")
async def cancel_task(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        task_row = conn.execute("SELECT status FROM search_tasks WHERE id=?", (task_id,)).fetchone()
    if not task_row:
        raise HTTPException(404, "搜索任务不存在")
    if task_row["status"] not in {"queued", "running"}:
        raise HTTPException(409, "任务已经结束，不能取消")
    task = running_tasks.get(task_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    running_tasks.pop(task_id, None)
    with connect() as conn:
        current = conn.execute("SELECT status FROM search_tasks WHERE id=?", (task_id,)).fetchone()
    if current and current["status"] in {"queued", "running"}:
        update_task(task_id, status="cancelled")
    return {"id": task_id, "status": "cancelled"}

@router.get("/api/search-tasks/{task_id}")
async def task_status(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        task = conn.execute("SELECT * FROM search_tasks WHERE id=?", (task_id,)).fetchone()
        summary = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
               FROM search_attempts WHERE task_id=?""", (task_id,),
        ).fetchone()
    if not task:
        raise HTTPException(404, "搜索任务不存在")
    result = dict(task)
    result["attempt_summary"] = dict(summary)
    return result

@router.get("/api/search-tasks/{task_id}/attempts")
async def task_attempts(task_id: int, limit: int = 500) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 2000))
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM search_tasks WHERE id=?", (task_id,)).fetchone():
            raise HTTPException(404, "搜索任务不存在")
        rows = conn.execute(
            """SELECT a.id,a.playlist_item_id,p.rank_no,p.original_title,a.site_id,a.site_name,
                      a.attempt_no,a.status,a.result_count,a.duration_ms,a.error_code,a.error_message,a.finished_at
               FROM search_attempts a JOIN playlist_items p ON p.id=a.playlist_item_id
               WHERE a.task_id=? ORDER BY a.id DESC LIMIT ?""", (task_id, safe_limit),
        ).fetchall()
        summary_rows = conn.execute(
            """SELECT site_id,site_name,COUNT(*) AS total,
                      SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                      CAST(AVG(duration_ms) AS INTEGER) AS average_ms
               FROM search_attempts WHERE task_id=? GROUP BY site_id,site_name ORDER BY site_name""", (task_id,),
        ).fetchall()
    return {"items": list(reversed(rows_to_dicts(rows))), "sites": rows_to_dicts(summary_rows)}

@router.post("/api/search-tasks/{task_id}/retry")
async def retry_task(task_id: int) -> dict[str, Any]:
    new_id, total = create_followup_search_task(task_id, True)
    running_tasks[new_id] = asyncio.create_task(run_search(new_id))
    return {"id": new_id, "status": "queued", "total": total, "parent_task_id": task_id}

@router.post("/api/search-tasks/{task_id}/restart")
async def restart_task(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        source = conn.execute("SELECT status FROM search_tasks WHERE id=?", (task_id,)).fetchone()
    if not source:
        raise HTTPException(404, "搜索任务不存在")
    if source["status"] in {"queued", "running"}:
        raise HTTPException(409, "任务仍在执行，无需重新启动")
    new_id, total = create_followup_search_task(task_id, False)
    running_tasks[new_id] = asyncio.create_task(run_search(new_id))
    return {"id": new_id, "status": "queued", "total": total, "parent_task_id": task_id}

@router.get("/api/search-tasks/{task_id}/logs")
async def task_logs(task_id: int, limit: int = 200) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 500))
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM search_tasks WHERE id=?", (task_id,)).fetchone():
            raise HTTPException(404, "搜索任务不存在")
        rows = conn.execute(
            "SELECT id,level,stage,message,created_at FROM search_task_logs WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task_id, safe_limit),
        ).fetchall()
    return list(reversed(rows_to_dicts(rows)))

@router.get("/api/candidates")
async def candidates(task_id: int, limit: int = MAX_CANDIDATE_RESPONSE) -> list[dict[str, Any]]:
    prune_raw_candidates()
    safe_limit = max(1, min(int(limit), MAX_CANDIDATE_RESPONSE))
    with connect() as conn:
        task = conn.execute("SELECT id,parent_task_id FROM search_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "搜索任务不存在")
        task_ids: list[int] = []
        seen_task_ids: set[int] = set()
        current_task_id: int | None = task_id
        while current_task_id and current_task_id not in seen_task_ids:
            seen_task_ids.add(current_task_id)
            task_ids.append(current_task_id)
            parent = conn.execute("SELECT parent_task_id FROM search_tasks WHERE id=?", (current_task_id,)).fetchone()
            current_task_id = int(parent["parent_task_id"]) if parent and parent["parent_task_id"] else None
        placeholders = ",".join("?" for _ in task_ids)
        rows = conn.execute(
            f"""SELECT c.*, p.rank_no, p.original_title, p.year, p.chinese_title,
                      p.tmdb_title,p.tmdb_original_title,p.tmdb_year,p.tmdb_imdb_id,
                      CASE WHEN cart.candidate_id IS NULL THEN 0 ELSE 1 END AS in_cart
               FROM candidates c JOIN playlist_items p ON p.id=c.playlist_item_id
               LEFT JOIN cart_items cart ON cart.candidate_id=c.id
               WHERE c.task_id IN ({placeholders}) ORDER BY p.rank_no, c.ranking LIMIT ?""",  # nosec B608
            (*task_ids, safe_limit),
        ).fetchall()
        site_rows = conn.execute("SELECT name,priority,icon_url FROM pt_sites").fetchall()
    site_profiles = {str(row["name"]).lower(): dict(row) for row in site_rows}
    result = rows_to_dicts(rows)
    for item in result:
        # Existing databases may contain pre-hardening raw URLs; sanitize on
        # read as well as at insert time so old rows cannot bypass the boundary.
        item["detail_url"] = safe_detail_url(item.get("detail_url"))
        item["context_available"] = item["id"] in raw_candidates
        try:
            item["metadata"] = json.loads(item.pop("metadata_json"))
        except (TypeError, json.JSONDecodeError):
            item["metadata"] = {}
        try:
            item["score_breakdown"] = json.loads(item.get("score_breakdown") or "[]")
        except json.JSONDecodeError:
            item["score_breakdown"] = []
        item["resource_key"] = item.get("resource_key") or resource_fingerprint(item["title"], item.get("size"))
        profile = site_profiles.get(str(item.get("site_name") or "").lower(), {})
        item["site_priority"] = int(profile.get("priority") or 100)
        item["site_icon"] = profile.get("icon_url") or ""
        factor = volume_factor_value(item["metadata"].get("volume_factor"))
        labels = [str(label).lower() for label in item["metadata"].get("labels", [])]
        item["volume_factor"] = factor
        item["is_free"] = factor == 0 or any(label in ("free", "免费", "freeleech") for label in labels)
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in result:
        groups.setdefault((int(item["playlist_item_id"]), item["resource_key"]), []).append(item)
    grouped: list[dict[str, Any]] = []
    for options in groups.values():
        # 同资源跨站点折叠：优先展示做种人数最多的发布（用户可实际下载），
        # 再做种相同或缺失时按站点优先级/免费/优惠排序作为次级规则。
        options.sort(key=lambda item: (
            -int(item.get("seeders") or 0), item["site_priority"], 0 if item["is_free"] else 1,
            item["volume_factor"], int(item.get("ranking") or 0),
        ))
        primary = dict(options[0])
        primary["site_count"] = len(options)
        primary["site_options"] = [{
            "id": option["id"], "site_name": option.get("site_name"), "seeders": option.get("seeders"),
            "size": option.get("size"), "is_free": option["is_free"], "site_priority": option["site_priority"],
            "volume_factor": option["volume_factor"], "labels": option["metadata"].get("labels", []),
            "in_cart": option.get("in_cart", 0), "context_available": option["context_available"],
            "detail_url": safe_detail_url(option.get("detail_url")), "publish_time": option["metadata"].get("publish_time"),
        } for option in options]
        factor_label = "免费" if primary["volume_factor"] == 0 else (f"下载 {int(primary['volume_factor'] * 100)}%" if primary["volume_factor"] < 1 else "普通")
        primary["site_selection_reason"] = (
            f"站点优先级 {primary['site_priority']} · {factor_label} · {int(primary.get('seeders') or 0)} 做种"
        )
        primary["in_cart"] = int(any(option.get("in_cart") for option in options))
        grouped.append(primary)
    grouped.sort(key=lambda item: (int(item["rank_no"] or 0), int(item.get("ranking") or 0)))
    return grouped
