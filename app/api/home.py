"""藏馆首页：馆藏进度、待办、进行中的任务与海报架。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..database import connect
from ..queries.films import playlist_items, playlist_summaries
from ..queries.sites import searchable_site_names
from ..services.films import (
    project_films,
    status_counts,
)
from ..services.library import hydrate_recent_emby_posters
from ..tasks import active_tasks
from ..util import to_int
from ..responses import HomeData

router = APIRouter()
# 首页海报架最多返回的影片数；界面按可用宽度只显示一整行。
HOME_SHELF_SIZE = 16

@router.get("/api/home", response_model=HomeData)
async def home(playlist_id: int | None = None) -> dict[str, Any]:
    with connect() as conn:
        playlists = playlist_summaries(conn)
        if playlist_id is None and playlists:
            playlist_id = to_int(playlists[0]["id"])
        if playlist_id is not None and not any(item["id"] == playlist_id for item in playlists):
            raise HTTPException(404, "片单不存在")
        items = playlist_items(conn, playlist_id) if playlist_id is not None else []
        failing_sites = searchable_site_names(conn, "error")
        empty_sites = searchable_site_names(conn, "empty")
        tasks = active_tasks(conn)
    projected = await project_films(items)
    counts = status_counts(projected)
    in_library = [item for item in items if item.get("library_state") == "in_library"]
    if settings.dashboard_random_posters:
        # “首页随机海报”：每天换一组，同一天内保持稳定。
        seed = to_int(datetime.now(timezone.utc).strftime("%Y%m%d"))
        recent_items = sorted(in_library, key=lambda item: (to_int(item["id"]) * 1103515245 + seed) & 0x7FFFFFFF)[:HOME_SHELF_SIZE]
    else:
        recent_items = sorted(in_library, key=lambda item: str(item.get("library_checked_at") or ""), reverse=True)[:HOME_SHELF_SIZE]
    await hydrate_recent_emby_posters(recent_items)
    recent_ids = [to_int(item["id"]) for item in recent_items]
    by_id = {film["id"]: film for film in await project_films(recent_items)} if recent_items else {}
    todos: list[dict[str, Any]] = []

    def add(key: str, count: int, **extra: Any) -> None:
        if count:
            todos.append({"key": key, "count": count, **extra})

    searchable = [film for film in projected if film["status"] == "missing" and "no_eligible" not in film["issues"]]
    add("search_missing", len(searchable))
    add("pick", counts["candidates"])
    add("submit", counts["selected"])
    add("unrecognized", counts["unrecognized"])
    add("unchecked", counts["unchecked"], emby_configured=bool(settings.emby_base_url and settings.emby_api_key))
    add("no_eligible", counts["issue:no_eligible"])
    add("submit_failed", counts["issue:submit_failed"])
    add("context_expired", counts["issue:context_expired"])
    add("failing_sites", len(failing_sites), names=failing_sites[:5])
    add("empty_sites", len(empty_sites), names=empty_sites[:5])
    return {
        "playlists": playlists,
        "playlist_id": playlist_id,
        "counts": counts,
        "todos": todos,
        "tasks": tasks,
        "recent": [by_id[item_id] for item_id in recent_ids if item_id in by_id],
        # 下一批寻片会按片单顺序处理的缺片（与“为缺片寻片”按钮的范围一致）。
        "up_next": searchable[:HOME_SHELF_SIZE],
    }
