"""AutoList HTTP routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from ..clients import EmbyClient, MoviePilotClient, TransmissionClient
from ..database import connect, json_value
from ..domain.titles import is_transmission_downloading, normalized_download_name, torrent_matches_item
from ..security import safe_error, sanitize_sensitive_text
from ..services.history import clear_download_history, playlist_item_snapshot, projected_download_history
from ..services.library import library_details
from ..state import (
    download_cart_lock,
    forget_raw_candidate,
    prune_raw_candidates,
    raw_candidates,
)
from ..util import first_value, resource_fingerprint, rows_to_dicts, utc_now

router = APIRouter()


def _moviepilot_success(response: Any) -> bool:
    """Parse MoviePilot's success flag without treating the string 'false' as true."""
    if not isinstance(response, dict):
        return False
    value = response.get("success")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().casefold() in {"true", "1", "yes", "ok", "success"}


def _matches_active_torrent(candidate: Any, item: dict[str, Any], active_torrents: list[dict[str, Any]]) -> bool:
    """Return True when the candidate's release already has an active Transmission task."""
    candidate_name = normalized_download_name(candidate["title"])
    for torrent in active_torrents:
        torrent_name = normalized_download_name(first_value(torrent, ("name", "torrent_name"), ""))
        if candidate_name and torrent_name and candidate_name == torrent_name:
            return True
        if torrent_matches_item(item, str(first_value(torrent, ("name", "torrent_name"), ""))):
            return True
    return False

@router.post("/api/cart/items/{candidate_id}")
async def toggle_cart(candidate_id: str) -> dict[str, Any]:
    prune_raw_candidates()
    with connect() as conn:
        candidate = conn.execute(
            "SELECT eligibility,exclusion_reason,playlist_item_id,site_name,resource_key FROM candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        if not candidate:
            raise HTTPException(404, "候选不存在")
        exists = conn.execute("SELECT 1 FROM cart_items WHERE candidate_id=?", (candidate_id,)).fetchone()
        if exists:
            conn.execute("DELETE FROM cart_items WHERE candidate_id=?", (candidate_id,))
            return {"candidate_id": candidate_id, "in_cart": False}
        if candidate["eligibility"] != "eligible":
            raise HTTPException(422, f"该资源已被电影策略排除：{candidate['exclusion_reason'] or '不符合允许组合'}")
        if candidate_id not in raw_candidates:
            raise HTTPException(409, "该候选的搜索上下文已失效，请重新搜索后再加入下载列表")
        # 同一影片、同一站点、同一发布已入车时不重复加入，避免跨任务重复提交。
        duplicate = conn.execute(
            """SELECT cart.candidate_id FROM cart_items cart
               JOIN candidates c ON c.id=cart.candidate_id
               WHERE c.playlist_item_id=? AND c.site_name=? AND COALESCE(c.resource_key,'')=?
               LIMIT 1""",
            (candidate["playlist_item_id"], candidate["site_name"], candidate["resource_key"] or ""),
        ).fetchone()
        if duplicate:
            raise HTTPException(422, "该发布已在下载列表中（相同影片与站点），请先移除现有条目")
        conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", (candidate_id, utc_now()))
        return {"candidate_id": candidate_id, "in_cart": True}

@router.get("/api/cart")
async def cart() -> list[dict[str, Any]]:
    prune_raw_candidates()
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.site_name, c.size, c.resolution, c.library_state, c.detail_url,
                      p.original_title, p.rank_no, p.chinese_title, p.tmdb_title, p.tmdb_original_title, p.tmdb_year, p.year
               FROM cart_items cart JOIN candidates c ON c.id=cart.candidate_id
               JOIN playlist_items p ON p.id=c.playlist_item_id ORDER BY cart.selected_at"""
        ).fetchall()
    items = rows_to_dicts(rows)
    for item in items:
        item["context_available"] = item["id"] in raw_candidates
    return items

@router.post("/api/cart/download")
async def download_cart() -> dict[str, Any]:
    prune_raw_candidates()
    if download_cart_lock.locked():
        raise HTTPException(409, "下载列表正在提交，请勿重复操作")
    async with download_cart_lock:
        with connect() as conn:
            rows = conn.execute(
                """SELECT c.*, p.original_title AS playlist_original_title,p.chinese_title AS playlist_chinese_title,
                          p.year AS playlist_year,p.imdb_id AS playlist_imdb_id,p.tmdb_id AS playlist_tmdb_id,
                          p.tmdb_title AS playlist_tmdb_title,p.tmdb_original_title AS playlist_tmdb_original_title,
                          p.tmdb_year AS playlist_tmdb_year,p.tmdb_imdb_id AS playlist_tmdb_imdb_id,
                          p.library_state AS playlist_library_state,p.library_checked_at AS playlist_library_checked_at,
                          p.id AS playlist_snapshot_id
                   FROM cart_items cart JOIN candidates c ON c.id=cart.candidate_id
                   JOIN playlist_items p ON p.id=c.playlist_item_id ORDER BY cart.selected_at"""
            ).fetchall()
        if not rows:
            raise HTTPException(422, "下载列表为空")
        try:
            current_torrents = await asyncio.wait_for(TransmissionClient().current_downloads(), timeout=6)
        except Exception:
            current_torrents = []
        active_torrents = [torrent for torrent in current_torrents if is_transmission_downloading(torrent)]
        emby = EmbyClient()
        moviepilot, completed, needs_research, submitted_tasks, expired_items = MoviePilotClient(), 0, 0, [], []
        skipped: list[dict[str, Any]] = []
        for candidate in rows:
            item_snapshot = playlist_item_snapshot({
                "id": candidate["playlist_snapshot_id"],
                "imdb_id": candidate["playlist_imdb_id"],
                "original_title": candidate["playlist_original_title"],
                "chinese_title": candidate["playlist_chinese_title"],
                "year": candidate["playlist_year"],
                "tmdb_id": candidate["playlist_tmdb_id"],
                "tmdb_title": candidate["playlist_tmdb_title"],
                "tmdb_original_title": candidate["playlist_tmdb_original_title"],
                "tmdb_year": candidate["playlist_tmdb_year"],
                "tmdb_imdb_id": candidate["playlist_tmdb_imdb_id"],
                "library_state": candidate["playlist_library_state"],
                "library_checked_at": candidate["playlist_library_checked_at"],
            })
            raw = raw_candidates.get(candidate["id"])
            if not raw:
                needs_research += 1
                expired_items.append({"candidate_id": candidate["id"], "title": candidate["playlist_original_title"]})
                message = "搜索上下文已失效，请重新搜索后加入下载列表"
                with connect() as conn:
                    already_recorded = conn.execute(
                        "SELECT 1 FROM download_history WHERE candidate_id=? AND success=0 AND message=? LIMIT 1",
                        (candidate["id"], message),
                    ).fetchone()
                    if not already_recorded:
                        conn.execute(
                            """INSERT INTO download_history(
                                   candidate_id,playlist_item_id,playlist_item_snapshot_json,title,torrent_name,site_name,success,message,created_at
                               ) VALUES(?,?,?,?,?,?,?,?,?)""",
                            (candidate["id"], candidate["playlist_item_id"], json_value(item_snapshot), candidate["playlist_original_title"], candidate["title"], candidate["site_name"], 0, message, utc_now()),
                        )
                continue
            # 幂等复查 1：相同发布已成功提交过（同候选或同影片+站点+资源指纹），跳过避免重复下载。
            resource_key = candidate["resource_key"] or resource_fingerprint(candidate["title"], candidate["size"])
            with connect() as conn:
                already_submitted = conn.execute(
                    """SELECT 1 FROM download_history h
                       WHERE h.success=1 AND (
                         h.candidate_id=? OR EXISTS (
                           SELECT 1 FROM candidates c WHERE c.id=h.candidate_id
                             AND c.playlist_item_id=? AND c.site_name=? AND c.resource_key=?
                         )
                       ) LIMIT 1""",
                    (candidate["id"], candidate["playlist_item_id"], candidate["site_name"], resource_key),
                ).fetchone()
            if already_submitted:
                skipped.append({"candidate_id": candidate["id"], "title": candidate["playlist_original_title"], "reason": "该发布已提交过"})
                continue
            # 幂等复查 2：相同发布已在 Transmission 下载中（含查询失败时的静默降级保护），跳过。
            playlist_item = {
                "id": candidate["playlist_snapshot_id"], "imdb_id": candidate["playlist_imdb_id"],
                "original_title": candidate["playlist_original_title"], "chinese_title": candidate["playlist_chinese_title"],
                "year": candidate["playlist_year"], "tmdb_id": candidate["playlist_tmdb_id"],
                "tmdb_title": candidate["playlist_tmdb_title"], "tmdb_original_title": candidate["playlist_tmdb_original_title"],
                "tmdb_year": candidate["playlist_tmdb_year"], "tmdb_imdb_id": candidate["playlist_tmdb_imdb_id"],
            }
            if _matches_active_torrent(candidate, playlist_item, active_torrents):
                skipped.append({"candidate_id": candidate["id"], "title": candidate["playlist_original_title"], "reason": "Transmission 正在下载"})
                continue
            # 幂等复查 3：搜索期间 Emby 状态未知（故障窗口）时，提交前复查实体库。
            if candidate["playlist_library_state"] == "unknown":
                state, _, _ = await library_details(
                    emby,
                    str(candidate["playlist_tmdb_title"] or candidate["playlist_chinese_title"] or candidate["playlist_original_title"]),
                    candidate["playlist_tmdb_year"] or candidate["playlist_year"],
                    candidate["playlist_tmdb_id"], candidate["playlist_tmdb_imdb_id"] or candidate["playlist_imdb_id"],
                )
                if state == "in_library":
                    skipped.append({"candidate_id": candidate["id"], "title": candidate["playlist_original_title"], "reason": "影片已入库"})
                    continue
            try:
                if not raw.get("media"):
                    raise RuntimeError("缺少媒体信息，无法应用 MoviePilot 分类规则")
                # 固定走 MoviePilot DownloadChain：它补全 TMDB 媒体信息、按 MP 分类目录选择路径，
                # 再交由 Transmission 写入 MOVIEPILOT 与站点标签，供 MP 后续整理。
                response = await moviepilot.download(raw["media"], raw["torrent"], downloader="Transmission")
                success = _moviepilot_success(response)
                if not isinstance(response, dict):
                    message = "MoviePilot 返回格式无效，未确认提交成功"
                    submission_hash = None
                elif success:
                    message = response.get("message") or response.get("hash")
                    submission_hash = str(response.get("hash") or "").strip() or None
                    submitted_tasks.append({"candidate_id": candidate["id"], "hash": submission_hash, "mode": "moviepilot"})
                else:
                    message = response.get("message") or "MoviePilot 未确认提交成功"
                    submission_hash = None
                message = sanitize_sensitive_text(message) if message else None
            except Exception as exc:
                success, message, submission_hash = False, safe_error(exc), None
            with connect() as conn:
                conn.execute(
                    """INSERT INTO download_history(
                           candidate_id,playlist_item_id,playlist_item_snapshot_json,title,torrent_name,site_name,submission_hash,success,message,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (candidate["id"], candidate["playlist_item_id"], json_value(item_snapshot), candidate["playlist_original_title"], candidate["title"], candidate["site_name"], submission_hash, int(success), message, utc_now()),
                )
                if success:
                    conn.execute("DELETE FROM cart_items WHERE candidate_id=?", (candidate["id"],))
                    completed += 1
                    forget_raw_candidate(candidate["id"])
        if needs_research and completed == 0 and not skipped:
            raise HTTPException(409, f"下载列表中 {needs_research} 个资源的搜索上下文已失效，请重新搜索后加入下载列表")
        return {
            "submitted": completed, "needs_research": needs_research, "expired_items": expired_items,
            "skipped": skipped, "mode": "moviepilot", "tasks": submitted_tasks,
        }

@router.get("/api/history")
async def history() -> list[dict[str, Any]]:
    return await projected_download_history()


@router.delete("/api/history")
async def delete_history(status: str = "all") -> dict[str, Any]:
    try:
        deleted = await clear_download_history(status)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"deleted": deleted, "status": status}
