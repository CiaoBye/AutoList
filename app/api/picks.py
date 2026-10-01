"""挑选台：有候选、已选定与无合格资源的影片。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..database import connect
from ..queries.films import playlist_items, playlist_summaries
from ..queries.search import active_search_progress
from ..queries.sites import site_display_rows
from ..services.candidates import candidate_view, latest_candidate_rows
from ..services.films import (
    project_films,
)
from ..responses import PickPage

router = APIRouter()


PICK_BUCKETS = ("candidates", "selected", "no_eligible")


def _pick_bucket(film: dict[str, Any]) -> str | None:
    if film["status"] in {"candidates", "selected"}:
        return film["status"]
    if film["status"] == "missing" and "no_eligible" in film["issues"]:
        return "no_eligible"
    return None


@router.get("/api/picks", response_model=PickPage)
async def picks(playlist_id: int | None = None, status: str = "all") -> dict[str, Any]:
    """挑选台：有候选、已选定与无合格资源的影片，按片单顺序，附带候选行。"""
    if status != "all" and status not in PICK_BUCKETS:
        raise HTTPException(422, "不支持的挑选筛选")
    with connect() as conn:
        playlists = playlist_summaries(conn)
        if playlist_id is not None and not any(item["id"] == playlist_id for item in playlists):
            raise HTTPException(404, "片单不存在")
        items = playlist_items(conn, playlist_id)
        searching = active_search_progress(conn, playlist_id)
    projected = await project_films(items)
    pickable = [film for film in projected if _pick_bucket(film)]
    counts = {"all": len(pickable), **{bucket: sum(1 for film in pickable if _pick_bucket(film) == bucket) for bucket in PICK_BUCKETS}}
    chosen = [film for film in pickable if status == "all" or _pick_bucket(film) == status]
    with connect() as conn:
        rows_by_item = latest_candidate_rows(conn, [film["id"] for film in chosen])
        site_rows = site_display_rows(conn)
    return {
        "items": [
            {**film, "bucket": _pick_bucket(film), **candidate_view(rows_by_item.get(film["id"], []), site_rows)}
            for film in chosen
        ],
        "counts": counts,
        "playlists": playlists,
        "searching": searching,
    }
