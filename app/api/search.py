"""AutoList HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..database import connect
from ..queries import search as queries
from ..queries.sites import searchable_site_ids
from ..schemas import TaskPayload
from ..services.search import (
    MAX_SEARCH_ITEMS,
    begin_search_task_slot,
    create_followup_search_task,
    run_search,
    searchable_playlist_items,
    update_task,
)
from ..tasks import SEARCH
from ..util import to_int
from ..responses import SearchAttempts, SearchTask, SearchTaskCancelled, SearchTaskLog, SearchTaskStarted

router = APIRouter()

@router.post("/api/search-tasks", response_model=SearchTaskStarted)
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
        # pending 按已过滤的队列计数，避免区间内已入库的影片误报超过上限。
        if payload.scope != "pending":
            item_ids =queries.item_ids_in_rank_range(conn, payload.playlist_id, range_start, range_end)
            if not item_ids:
                raise HTTPException(422, "所选范围没有影片")
        if len(item_ids) > MAX_SEARCH_ITEMS:
            raise HTTPException(413, f"单次搜索最多处理 {MAX_SEARCH_ITEMS} 部影片")
        site_ids = searchable_site_ids(conn)
        if not site_ids:
            raise HTTPException(422, "请先在站点配置中选择至少一个参与搜索的站点")
        task_id = queries.insert_search_task(
            conn, payload.playlist_id, range_start=range_start, range_end=range_end,
            trigger="pending" if payload.scope == "pending" else "manual", site_ids=site_ids, item_ids=item_ids,
        )
    SEARCH.start(task_id, run_search(task_id))
    return {"id": task_id, "status": "queued", "total": len(item_ids), "scope": payload.scope}

@router.get("/api/search-tasks", response_model=list[SearchTask])
async def search_tasks(playlist_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 100))
    with connect() as conn:
        return queries.recent_search_tasks(conn, playlist_id, safe_limit)

@router.post("/api/search-tasks/{task_id}/cancel", response_model=SearchTaskCancelled)
async def cancel_task(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        task_row = queries.get_search_task(conn, task_id)
    if not task_row:
        raise HTTPException(404, "搜索任务不存在")
    if task_row["status"] not in {"queued", "running"}:
        raise HTTPException(409, "任务已经结束，不能取消")
    await SEARCH.cancel(task_id)
    with connect() as conn:
        current = queries.get_search_task(conn, task_id)
    if current and current["status"] in {"queued", "running"}:
        update_task(task_id, status="cancelled")
    return {"id": task_id, "status": "cancelled"}

@router.get("/api/search-tasks/{task_id}", response_model=SearchTask)
async def task_status(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        task = queries.get_search_task(conn, task_id)
        if not task:
            raise HTTPException(404, "搜索任务不存在")
        return {**dict(task), "attempt_summary": queries.attempt_summary(conn, task_id)}

@router.get("/api/search-tasks/{task_id}/attempts", response_model=SearchAttempts)
async def task_attempts(task_id: int, limit: int = 500) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 2000))
    with connect() as conn:
        if not queries.get_search_task(conn, task_id):
            raise HTTPException(404, "搜索任务不存在")
        return {"items": queries.task_attempts(conn, task_id, safe_limit), "sites": queries.task_site_summaries(conn, task_id)}

@router.post("/api/search-tasks/{task_id}/retry", response_model=SearchTaskStarted)
async def retry_task(task_id: int) -> dict[str, Any]:
    new_id, total = create_followup_search_task(task_id, True)
    SEARCH.start(new_id, run_search(new_id))
    return {"id": new_id, "status": "queued", "total": total, "parent_task_id": task_id}

@router.post("/api/search-tasks/{task_id}/restart", response_model=SearchTaskStarted)
async def restart_task(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        source = queries.get_search_task(conn, task_id)
    if not source:
        raise HTTPException(404, "搜索任务不存在")
    if source["status"] in {"queued", "running"}:
        raise HTTPException(409, "任务仍在执行，无需重新启动")
    new_id, total = create_followup_search_task(task_id, False)
    SEARCH.start(new_id, run_search(new_id))
    return {"id": new_id, "status": "queued", "total": total, "parent_task_id": task_id}

@router.get("/api/search-tasks/{task_id}/logs", response_model=list[SearchTaskLog])
async def task_logs(task_id: int, limit: int = 200) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 500))
    with connect() as conn:
        if not queries.get_search_task(conn, task_id):
            raise HTTPException(404, "搜索任务不存在")
        return queries.task_logs(conn, task_id, safe_limit)
