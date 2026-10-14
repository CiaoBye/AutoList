"""挑选台：有候选、已选定与无合格资源的影片。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from ..clients import EmbyClient
from ..config import settings
from ..database import connect
from ..queries.films import playlist_items, playlist_summaries, submitted_downloads
from ..queries.search import active_search_progress
from ..queries.sites import site_display_rows
from ..services.candidates import candidate_view, latest_candidate_rows
from ..services.films import (
    project_films,
)
from ..candidate_policy import parse_resolution
from ..services.history import transfer_info
from ..services.search import resolution_height
from ..services.transmission_snapshot import _current_downloads_cached
from ..util import to_int
from ..responses import PickPage
from ..services.identity import identity_flag

router = APIRouter()


PICK_BUCKETS = ("candidates", "selected", "upgrade", "no_eligible", "stalled")


def _pick_bucket(film: dict[str, Any]) -> str | None:
    if film["status"] in {"candidates", "selected"}:
        return film["status"]
    # 下载停滞超过 24 小时：回到挑选台，换一个资源。
    if film["status"] == "downloading" and "download_stalled" in film["issues"]:
        return "stalled"
    if film["status"] == "missing" and "no_eligible" in film["issues"]:
        return "no_eligible"
    # 已入馆影片的升级搜索找到了更高画质的候选：候选里只有高于现有画质的资源。
    if film["status"] == "in_library" and "upgrade_ready" in film["issues"]:
        return "upgrade"
    return None


async def _current_releases(films: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """升级的影片：Emby 里现有的版本；下载停滞的影片：原来提交的、还在 Transmission 里的下载。读取失败的留空。"""
    current: dict[int, list[dict[str, Any]]] = {}
    upgrades = [film for film in films if film["bucket"] == "upgrade"]
    stalled = [film for film in films if film["bucket"] == "stalled"]
    if upgrades and settings.emby_base_url and settings.emby_api_key:
        async def versions(film: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
            try:
                found = await EmbyClient().versions(film.get("tmdb_id"), film.get("imdb_id"))
            except Exception:
                found = []
            return film["id"], [
                {"kind": "library", "name": item["name"], "size": item["size"], "resolution": _tier_text(item)} for item in found
            ]

        for item_id, releases in await asyncio.gather(*(versions(film) for film in upgrades)):
            current[item_id] = releases
    if stalled:
        try:
            torrents, _state = await _current_downloads_cached()
        except Exception:
            torrents = []
        by_hash = {str(torrent.get("hashString") or "").lower(): torrent for torrent in torrents}
        with connect() as conn:
            submitted = submitted_downloads(conn, [film["id"] for film in stalled])
        for film in stalled:
            releases = []
            for record in submitted.get(film["id"], []):
                torrent = by_hash.get(str(record["submission_hash"]).lower())
                if not torrent:
                    continue
                info = transfer_info(torrent)
                releases.append({
                    "kind": "download", "name": str(torrent.get("name") or ""), "size": to_int(torrent.get("totalSize")) or None,
                    "site": record["site_name"], "percent": info["percent"],
                    "idle_hours": round(info["idle_hours"]) if info["idle_hours"] is not None else None,
                })
            current[film["id"]] = releases
    return current


def _original_height(releases: list[dict[str, Any]]) -> int | None:
    """原来的下载里最高的画质（按种子名解析）。"""
    heights = [resolution_height(parse_resolution(str(release["name"]).upper())) for release in releases if release["kind"] == "download"]
    return max((height for height in heights if height), default=None)


def _tier_text(version: dict[str, Any]) -> str | None:
    return f"{version['tier']}p" if version.get("tier") else None


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
    # “全部”只放需要你动手的；等待资源的影片按自己的退避自动复查，点“等待资源”才看。
    actionable = [film for film in pickable if _pick_bucket(film) != "no_eligible"]
    counts = {"all": len(actionable), **{bucket: sum(1 for film in pickable if _pick_bucket(film) == bucket) for bucket in PICK_BUCKETS}}
    chosen = actionable if status == "all" else [film for film in pickable if _pick_bucket(film) == status]
    next_search = {item["id"]: item.get("next_search_at") for item in items}
    flags = {item["id"]: identity_flag(item) for item in items}
    with connect() as conn:
        rows_by_item = latest_candidate_rows(conn, [film["id"] for film in chosen])
        site_rows = site_display_rows(conn)
    for film in chosen:
        film["bucket"] = _pick_bucket(film)
    current = await _current_releases(chosen)
    views = {film["id"]: candidate_view(rows_by_item.get(film["id"], []), site_rows) for film in chosen}
    for film in chosen:
        # 换掉停滞的下载时，低于原来那条的画质不再列出（原来是 2160p 就不给 1080p）。
        floor = _original_height(current.get(film["id"], []))
        if film["bucket"] == "stalled" and floor:
            kept = [c for c in views[film["id"]]["candidates"] if (resolution_height(c.get("resolution")) or floor) >= floor]
            views[film["id"]] = {**views[film["id"]], "candidates": kept}
    return {
        "items": [
            {
                **film, "bucket": film["bucket"], "current": current.get(film["id"], []), "next_search_at": next_search.get(film["id"]), "identity_flag": flags.get(film["id"]),
                **views[film["id"]],
            }
            for film in chosen
        ],
        "counts": counts,
        "playlists": playlists,
        "searching": searching,
    }
