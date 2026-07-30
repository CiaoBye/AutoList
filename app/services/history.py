"""Download history lifecycle projection from MoviePilot / Transmission / Emby signals."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from ..clients import TransmissionClient
from ..database import connect
from ..security import sanitize_sensitive_text
from ..util import first_value, rows_to_dicts, utc_now
from ..domain.titles import is_transmission_downloading, normalized_download_name, strict_torrent_matches_item


DOWNLOAD_LIFECYCLE_LABELS = {
    "submitted": "已提交",
    "downloading": "下载中",
    "pending_confirmation": "待确认",
    "pending_library": "待入库",
    "organized": "已整理/已入库",
    "failed": "失败",
}


def playlist_item_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only non-sensitive movie identity fields for history fallback."""
    return {
        "id": row.get("id"),
        "imdb_id": row.get("imdb_id"),
        "original_title": row.get("original_title"),
        "year": row.get("year"),
        "chinese_title": row.get("chinese_title"),
        "tmdb_id": row.get("tmdb_id"),
        "tmdb_title": row.get("tmdb_title"),
        "tmdb_original_title": row.get("tmdb_original_title"),
        "tmdb_year": row.get("tmdb_year"),
        "tmdb_imdb_id": row.get("tmdb_imdb_id"),
        "library_state": row.get("library_state"),
        "library_checked_at": row.get("library_checked_at"),
    }


def _history_item_snapshot(row: dict[str, Any]) -> dict[str, Any] | None:
    current_item_id = row.get("current_playlist_item_id")
    if current_item_id is not None:
        return {
            "id": current_item_id,
            "imdb_id": row.get("playlist_imdb_id"),
            "original_title": row.get("playlist_original_title"),
            "chinese_title": row.get("playlist_chinese_title"),
            "year": row.get("playlist_year"),
            "tmdb_id": row.get("playlist_tmdb_id"),
            "tmdb_title": row.get("playlist_tmdb_title"),
            "tmdb_original_title": row.get("playlist_tmdb_original_title"),
            "tmdb_year": row.get("playlist_tmdb_year"),
            "tmdb_imdb_id": row.get("playlist_tmdb_imdb_id"),
            "library_state": row.get("playlist_library_state"),
            "library_checked_at": row.get("playlist_library_checked_at"),
        }
    raw_snapshot = row.get("playlist_item_snapshot_json")
    if not raw_snapshot:
        return None
    try:
        snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else raw_snapshot
    except (TypeError, json.JSONDecodeError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _history_torrent_matches(row: dict[str, Any], torrent: dict[str, Any]) -> bool:
    torrent_hash = str(torrent.get("hashString") or "").strip().casefold()
    submission_hash = str(row.get("submission_hash") or "").strip().casefold()
    if torrent_hash and submission_hash and torrent_hash == submission_hash:
        return True
    torrent_name = normalized_download_name(first_value(torrent, ("name", "torrent_name"), ""))
    history_name = normalized_download_name(row.get("torrent_name"))
    if torrent_name and history_name and torrent_name == history_name:
        return True
    item = row.get("_playlist_item")
    return bool(item and strict_torrent_matches_item(item, str(first_value(torrent, ("name", "torrent_name"), ""))))


def _active_history_matches(
    histories: list[dict[str, Any]], torrents: list[dict[str, Any]],
) -> tuple[set[int], set[int]]:
    matched: set[int] = set()
    ambiguous: set[int] = set()
    eligible = [row for row in histories if bool(row.get("success"))]
    active_torrents = [torrent for torrent in torrents if is_transmission_downloading(torrent)]
    for torrent in active_torrents:
        torrent_hash = str(torrent.get("hashString") or "").strip().casefold()
        hash_matches = [
            row for row in eligible
            if torrent_hash and str(row.get("submission_hash") or "").strip().casefold() == torrent_hash
        ]
        if len(hash_matches) == 1:
            matched.add(int(hash_matches[0]["id"]))
            continue
        if len(hash_matches) > 1:
            ambiguous.update(int(row["id"]) for row in hash_matches)
            continue
        name = normalized_download_name(first_value(torrent, ("name", "torrent_name"), ""))
        name_matches = [
            row for row in eligible
            if name and normalized_download_name(row.get("torrent_name")) == name
        ]
        if len(name_matches) == 1:
            matched.add(int(name_matches[0]["id"]))
            continue
        if len(name_matches) > 1:
            ambiguous.update(int(row["id"]) for row in name_matches)
            continue
        identity_matches = [row for row in eligible if _history_torrent_matches(row, torrent)]
        if len(identity_matches) == 1:
            matched.add(int(identity_matches[0]["id"]))
        elif len(identity_matches) > 1:
            ambiguous.update(int(row["id"]) for row in identity_matches)
    return matched, ambiguous


def _project_history_state(
    row: dict[str, Any], matched_ids: set[int], ambiguous_ids: set[int],
    transmission_error: bool, checked_at: str,
) -> dict[str, Any]:
    history_id = int(row["id"])
    library_state = str(row.get("playlist_library_state") or "unknown")
    if not bool(row.get("success")):
        lifecycle_status, source, reason, next_action = (
            "failed", "AutoList/MoviePilot", row.get("message") or "提交失败", "查看失败原因",
        )
        status_checked_at = row.get("created_at")
    elif library_state == "in_library":
        lifecycle_status, source, reason, next_action = (
            "organized", "Emby", "Emby 已找到实体媒体", "无需操作",
        )
        status_checked_at = row.get("playlist_library_checked_at") or checked_at
    elif library_state == "strm":
        lifecycle_status, source, reason, next_action = (
            "pending_library", "Emby", "Emby 已找到 .strm，实体媒体尚未确认", "刷新 Emby 状态",
        )
        status_checked_at = row.get("playlist_library_checked_at") or checked_at
    elif history_id in matched_ids:
        lifecycle_status, source, reason, next_action = (
            "downloading", "Transmission", "Transmission 正在下载", "等待下游确认",
        )
        status_checked_at = checked_at
    elif transmission_error or history_id in ambiguous_ids or (
        library_state in {"not_found", "unknown"} and row.get("playlist_library_checked_at")
    ):
        lifecycle_status, source, reason, next_action = (
            "pending_confirmation", "Transmission/Emby", "暂时无法确认下游状态", "稍后刷新状态",
        )
        status_checked_at = row.get("playlist_library_checked_at") or checked_at
    else:
        lifecycle_status, source, reason, next_action = (
            "submitted", "MoviePilot", "已提交给 MoviePilot，等待下游服务确认", "等待下游确认",
        )
        status_checked_at = row.get("created_at")
    item = {key: value for key, value in row.items() if not key.startswith("_")}
    for key in (
        "resolved_playlist_item_id", "playlist_original_title", "playlist_chinese_title", "playlist_year",
        "playlist_imdb_id", "playlist_tmdb_id", "playlist_tmdb_title", "playlist_tmdb_original_title",
        "playlist_tmdb_year", "playlist_tmdb_imdb_id", "playlist_library_state", "playlist_library_checked_at",
        "playlist_item_id", "playlist_item_snapshot_json", "submission_hash", "current_playlist_item_id",
    ):
        item.pop(key, None)
    item["lifecycle_status"] = lifecycle_status
    item["status_label"] = DOWNLOAD_LIFECYCLE_LABELS[lifecycle_status]
    item["status_source"] = source
    item["status_reason"] = sanitize_sensitive_text(str(reason), 500)
    item["status_checked_at"] = status_checked_at
    item["next_action"] = next_action
    if item.get("message"):
        item["message"] = sanitize_sensitive_text(item["message"])
    return item


async def projected_download_history(limit: int = 200) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 5000))
    with connect() as conn:
        rows = conn.execute(
            """SELECT h.*, COALESCE(h.playlist_item_id,c.playlist_item_id) AS resolved_playlist_item_id,
                      p.id AS current_playlist_item_id,
                      p.imdb_id AS playlist_imdb_id,p.original_title AS playlist_original_title,
                      p.chinese_title AS playlist_chinese_title,p.year AS playlist_year,
                      p.tmdb_id AS playlist_tmdb_id,p.tmdb_title AS playlist_tmdb_title,
                      p.tmdb_original_title AS playlist_tmdb_original_title,p.tmdb_year AS playlist_tmdb_year,
                      p.tmdb_imdb_id AS playlist_tmdb_imdb_id,
                      p.library_state AS playlist_library_state,p.library_checked_at AS playlist_library_checked_at
               FROM download_history h
               LEFT JOIN candidates c ON c.id=h.candidate_id
               LEFT JOIN playlist_items p ON p.id=COALESCE(h.playlist_item_id,c.playlist_item_id)
               ORDER BY h.id DESC LIMIT ?""",
            (safe_limit,),
        ).fetchall()
    histories = rows_to_dicts(rows)
    if not histories:
        return []
    for row in histories:
        row["_playlist_item"] = _history_item_snapshot(row)
        snapshot = row["_playlist_item"]
        if row.get("playlist_library_state") is None and snapshot:
            row["playlist_library_state"] = snapshot.get("library_state")
        if row.get("playlist_library_checked_at") is None and snapshot:
            row["playlist_library_checked_at"] = snapshot.get("library_checked_at")
    transmission_error = False
    try:
        torrents = await asyncio.wait_for(TransmissionClient().current_downloads(), timeout=6)
    except Exception:
        torrents = []
        transmission_error = True
    matched_ids, ambiguous_ids = _active_history_matches(histories, torrents)
    checked_at = utc_now()
    return [_project_history_state(row, matched_ids, ambiguous_ids, transmission_error, checked_at) for row in histories]


async def clear_download_history(status: str) -> int:
    allowed = set(DOWNLOAD_LIFECYCLE_LABELS) | {"all"}
    if status not in allowed:
        raise ValueError("无效的下载历史分组")
    with connect() as conn:
        if status == "all":
            return conn.execute("DELETE FROM download_history").rowcount
    items = await projected_download_history(limit=5000)
    ids = [int(item["id"]) for item in items if item.get("lifecycle_status") == status]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with connect() as conn:
        return conn.execute(
            f"DELETE FROM download_history WHERE id IN ({placeholders})",  # nosec B608
            ids,
        ).rowcount
