"""AutoList HTTP routes."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ..candidate_policy import merge_custom_rules, normalized_policy, release_group_catalog
from ..clients import MoviePilotClient
from ..config import load_runtime_settings, save_runtime_settings, settings
from ..cookiecloud import cookie_for_host, cookie_groups
from ..database import config_values, connect, json_value, save_config
from ..list_sources import PlaylistSourceFetcher
from ..schemas import (
    ConfigPayload,
    CookieCloudUploadPayload,
    ImportPayload,
    PlaylistAutomationPayload,
    PlaylistOrderPayload,
    PlaylistSyncPayload,
    PlaylistUpdatePayload,
    RuntimeSettingsPayload,
    ScorePreviewPayload,
    SitePayload,
    TaskPayload,
)
from ..security import safe_error, sanitize_sensitive_text
from ..services.automation import start_playlist_automation, sync_playlist_incremental, update_recognition_task
from ..services.cookiecloud_store import cookiecloud_file, stored_cookiecloud_payload
from ..services.history import clear_download_history, playlist_item_snapshot, projected_download_history
from ..services.imports import normalize_import_items, resolve_import
from ..services.library import run_library_scan
from ..services.recognition import analyze_candidate, persist_tmdb_item, recognize_movie
from ..services.search import (
    create_followup_search_task,
    run_search,
    searchable_playlist_items,
)
from ..services.sites import resolve_site_adapter, test_site_config
from ..state import (
    enforce_cookiecloud_rate_limit,
    enforce_search_task_capacity,
    poster_cache,
    raw_candidates,
    download_cart_lock,
    forget_raw_candidate,
    prune_raw_candidates,
    require_configured_cookiecloud_uuid,
    running_automation_tasks,
    running_library_tasks,
    running_recognition_tasks,
    running_tasks,
    site_icon_cache,
)
from ..util import (
    decode_cookiecloud_body,
    first_value,
    raster_image_media_type,
    resource_fingerprint,
    rows_to_dicts,
    secret_free,
    utc_now,
    validate_remote_icon_url,
    volume_factor_value,
)

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

@router.post("/api/cart/items/{candidate_id}")
async def toggle_cart(candidate_id: str) -> dict[str, Any]:
    prune_raw_candidates()
    with connect() as conn:
        candidate = conn.execute("SELECT eligibility,exclusion_reason FROM candidates WHERE id=?", (candidate_id,)).fetchone()
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
        conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", (candidate_id, utc_now()))
        return {"candidate_id": candidate_id, "in_cart": True}

@router.get("/api/cart")
async def cart() -> list[dict[str, Any]]:
    prune_raw_candidates()
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.site_name, c.size, c.resolution, c.library_state, p.original_title
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
        moviepilot, completed, needs_research, submitted_tasks, expired_items = MoviePilotClient(), 0, 0, [], []
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
        if needs_research and completed == 0:
            raise HTTPException(409, f"下载列表中 {needs_research} 个资源的搜索上下文已失效，请重新搜索后加入下载列表")
        return {
            "submitted": completed, "needs_research": needs_research, "expired_items": expired_items,
            "mode": "moviepilot", "tasks": submitted_tasks,
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
