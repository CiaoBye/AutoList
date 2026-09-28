"""AutoList HTTP routes."""

from __future__ import annotations

import asyncio
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
from ..state import running_tasks
from ..util import rows_to_dicts, to_int, utc_now

router = APIRouter()

@router.post("/api/search-tasks")
async def create_task(payload: TaskPayload) -> dict[str, Any]:
    if payload.scope == "range" and payload.range_end < payload.range_start:
        raise HTTPException(422, "结束序号不能小于起始序号")
    item_ids: list[int] = []
    if payload.scope == "pending":
        queue = await searchable_playlist_items(payload.playlist_id, payload.count)
        if queue.get("download_state") == "unknown":
            raise HTTPException(503, "无法确认 Transmission 当前下载任务，已暂停创建搜索任务")
        item_ids = [to_int(item["id"]) for item in queue["items"]]
        if not item_ids:
            raise HTTPException(422, "当前片单没有可搜索的未入库影片")
        range_start = min(to_int(item["rank_no"]) for item in queue["items"])
        range_end = max(to_int(item["rank_no"]) for item in queue["items"])
    else:
        range_start, range_end = payload.range_start, payload.range_end
    with connect() as conn:
        begin_search_task_slot(conn)
        if payload.scope == "pending":
            # pending 容量按已过滤队列计数（审计 3-1），避免区间内已入库条目误报 413。
            total = len(item_ids)
            selected_items = []
        else:
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
            to_int(row["id"]) for row in conn.execute(
                "SELECT id FROM pt_sites WHERE enabled=1 AND search_enabled=1 ORDER BY priority,id",
            ).fetchall()
        ]
        if not site_ids:
            raise HTTPException(422, "请先在站点配置中选择至少一个参与搜索的站点")
        if not item_ids:
            item_ids = [to_int(row["id"]) for row in selected_items]
        task_cursor = conn.execute(
            """INSERT INTO search_tasks(
                 playlist_id,range_start,range_end,status,total,trigger,site_ids_json,item_ids_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (payload.playlist_id, range_start, range_end, "queued", len(item_ids),
             "pending" if payload.scope == "pending" else "manual", json_value(site_ids), json_value(item_ids),
             utc_now(), utc_now()),
        )
        task_lastrowid = task_cursor.lastrowid
        if task_lastrowid is None:
            raise RuntimeError("搜索任务写入失败")
        task_id = task_lastrowid
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
        if result:
            # 一次 IN 查询合并各任务聚合，避免逐任务 N+1（审计 3-23）。
            task_ids = [task["id"] for task in result]
            placeholders = ",".join("?" for _ in task_ids)
            summaries = {
                row["task_id"]: dict(row)
                for row in conn.execute(
                    f"""SELECT task_id, COUNT(*) AS total,
                              SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                              SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
                       FROM search_attempts WHERE task_id IN ({placeholders}) GROUP BY task_id""",  # nosec B608
                    task_ids,
                ).fetchall()
            }
            for task in result:
                task["attempt_summary"] = summaries.get(
                    task["id"], {"total": 0, "succeeded": 0, "failed": 0},
                )
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
