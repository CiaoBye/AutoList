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
from ..clients import EmbyClient, MoviePilotClient, TMDBClient, TransmissionClient
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
    SiteCookiePayload,
    SitePayload,
    TaskPayload,
)
from ..security import safe_error, sanitize_sensitive_text
from ..services.automation import start_playlist_automation, sync_playlist_incremental, update_recognition_task
from ..services.cookiecloud_store import cookiecloud_file, stored_cookiecloud_payload
from ..services.history import projected_download_history
from ..services.imports import normalize_import_items, resolve_import
from ..services.library import run_library_scan
from ..services.recognition import analyze_candidate, persist_tmdb_item, recognize_movie
from ..services.search import (
    create_followup_search_task,
    run_search,
    searchable_playlist_items,
)
from ..services.sites import domain_match, moviepilot_site_snapshot, resolve_site_adapter, test_site_config
from ..state import (
    enforce_cookiecloud_rate_limit,
    enforce_search_task_capacity,
    moviepilot_site_ids,
    poster_cache,
    raw_candidates,
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

@router.get("/api/sites")
async def sites() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM pt_sites ORDER BY id").fetchall())
    try:
        mp_sites, statistics, users = await moviepilot_site_snapshot()
    except Exception:
        mp_sites, statistics, users = [], [], []
    for item in rows:
        item["api_key_configured"] = bool(item.get("api_key"))
        item["cookie_configured"] = bool(item.get("cookie"))
        item["api_key"] = ""
        item["cookie"] = ""
        item["rss_url_configured"] = bool(item.get("rss_url"))
        item["rss_url"] = ""
        mp_site = next((candidate for candidate in mp_sites if domain_match(str(candidate.get("domain") or candidate.get("url") or ""), item["base_url"])), None)
        stat = next((candidate for candidate in statistics if domain_match(str(candidate.get("domain") or ""), item["base_url"])), {})
        user = next((candidate for candidate in users if domain_match(str(candidate.get("domain") or ""), item["base_url"])), {})
        item["mp_site_id"] = mp_site.get("id") if mp_site else None
        if item["mp_site_id"]:
            moviepilot_site_ids[item["id"]] = int(item["mp_site_id"])
        item["icon_endpoint"] = f"/api/sites/{item['id']}/icon"
        item["mp_active"] = bool(mp_site.get("is_active")) if mp_site else bool(item["enabled"])
        item["mp_user"] = {key: user.get(key) for key in ("username", "user_level", "upload", "download", "ratio", "bonus", "seeding", "leeching", "updated_time", "err_msg")}
        if item["mp_user"].get("err_msg"):
            item["mp_user"]["err_msg"] = sanitize_sensitive_text(item["mp_user"]["err_msg"])
        item["mp_status"] = {key: stat.get(key) for key in ("seconds", "lst_state", "lst_mod_date", "success", "fail")}
    return rows

@router.get("/api/sites/{site_id}/icon")
async def site_icon(site_id: int) -> Response:
    """Serve MP's site icon locally so authenticated/private site favicons do not fail in the browser."""
    if site_id in site_icon_cache:
        content, media_type = site_icon_cache[site_id]
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
    with connect() as conn:
        row = conn.execute("SELECT id,base_url,icon_url FROM pt_sites WHERE id=?", (site_id,)).fetchone()
    if not row:
        raise HTTPException(404, "站点不存在")
    icon_value = str(row["icon_url"] or "")
    mp_id = moviepilot_site_ids.get(site_id)
    if not icon_value and not mp_id:
        try:
            mp_sites, _, _ = await moviepilot_site_snapshot()
            match = next((item for item in mp_sites if domain_match(str(item.get("domain") or item.get("url") or ""), row["base_url"])), None)
            mp_id = int(match["id"]) if match and match.get("id") else None
            if mp_id:
                moviepilot_site_ids[site_id] = mp_id
        except Exception:
            mp_id = None
    try:
        if mp_id:
            payload = await MoviePilotClient().site_icon(mp_id)
            data = payload.get("data", payload) if isinstance(payload, dict) else payload
            icon_value = str(data.get("icon") or data.get("url") or "") if isinstance(data, dict) else str(data or "")
        if icon_value.startswith("data:image/"):
            header, encoded = icon_value.split(",", 1)
            media_type = header.split(";", 1)[0].split(":", 1)[1]
            content = base64.b64decode(encoded)
        else:
            source = icon_value if icon_value.startswith(("http://", "https://")) else f"{str(row['base_url']).rstrip('/')}/favicon.ico"
            await validate_remote_icon_url(source, str(row["base_url"]))
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                upstream = await client.get(source)
                upstream.raise_for_status()
            await validate_remote_icon_url(str(upstream.url), str(row["base_url"]))
            if int(upstream.headers.get("content-length") or 0) > 2 * 1024 * 1024:
                raise RuntimeError("图标文件过大")
            content = upstream.content
            media_type = upstream.headers.get("content-type", "image/x-icon").split(";", 1)[0]
        detected_media_type = raster_image_media_type(content)
        if not content or len(content) > 2 * 1024 * 1024 or not detected_media_type:
            raise RuntimeError("图标内容无效")
        media_type = detected_media_type
        site_icon_cache[site_id] = (content, media_type)
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        # A deterministic SVG fallback still gives every site a consistent visual anchor.
        initial = (str(row["base_url"] or "?")[:1] or "?").upper()
        fallback = f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" rx="14" fill="#eeeaff"/><text x="32" y="42" text-anchor="middle" font-family="Arial" font-size="28" font-weight="700" fill="#6657e8">{initial}</text></svg>'.encode()
        return Response(content=fallback, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

@router.get("/api/sites/{site_id}/health-history")
async def site_health_history(site_id: int, limit: int = 50) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 200))
    with connect() as conn:
        site = conn.execute("SELECT id,name FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        if not site:
            raise HTTPException(404, "站点不存在")
        rows = conn.execute(
            """SELECT status,result_count,duration_ms,error_code,error_message,finished_at
               FROM search_attempts WHERE site_id=? ORDER BY id DESC LIMIT ?""", (site_id, safe_limit),
        ).fetchall()
        summary = conn.execute(
            """SELECT COUNT(*) AS total,SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                      CAST(AVG(duration_ms) AS INTEGER) AS average_ms,MAX(finished_at) AS last_attempt_at
               FROM search_attempts WHERE site_id=?""", (site_id,),
        ).fetchone()
    result = dict(summary)
    total = int(result["total"] or 0)
    result["success_rate"] = round(int(result["succeeded"] or 0) / total * 100, 1) if total else None
    return {"site": dict(site), "summary": result, "items": rows_to_dicts(rows)}

@router.post("/api/sites")
async def add_site(payload: SitePayload) -> dict[str, Any]:
    base_url = validated_base_url(payload.base_url, "站点地址", True)
    rss_url = validated_base_url(payload.rss_url, "RSS 地址", False)
    adapter = resolve_site_adapter(base_url, rss_url)
    try:
        with connect() as conn:
            site_id = conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,api_key,cookie,user_agent,priority,timeout_seconds,rss_url,icon_url,proxy,render,limit_interval,limit_count,enabled,search_enabled,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (payload.name.strip(), adapter, base_url, payload.api_key or "", payload.cookie or "", payload.user_agent,
                 payload.priority, payload.timeout_seconds, rss_url, payload.icon_url, int(payload.proxy), int(payload.render),
                 payload.limit_interval, payload.limit_count, int(payload.enabled), int(payload.search_enabled), utc_now()),
            ).lastrowid
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "站点名称已存在") from exc
    return {"id": site_id, "name": payload.name, "adapter": adapter, "enabled": payload.enabled, "search_enabled": payload.search_enabled}

@router.put("/api/sites/{site_id}")
async def update_site(site_id: int, payload: SitePayload) -> dict[str, Any]:
    base_url = validated_base_url(payload.base_url, "站点地址", True)
    with connect() as conn:
        current = conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        if not current:
            raise HTTPException(404, "站点不存在")
        rss_url = validated_base_url(payload.rss_url, "RSS 地址", False) if payload.rss_url.strip() else str(current["rss_url"] or "")
        adapter = resolve_site_adapter(base_url, rss_url)
        conn.execute(
            """UPDATE pt_sites SET name=?,adapter=?,base_url=?,api_key=?,cookie=?,user_agent=?,priority=?,timeout_seconds=?,rss_url=?,icon_url=?,proxy=?,render=?,limit_interval=?,limit_count=?,enabled=?,search_enabled=?,migration_note=NULL WHERE id=?""",
            (payload.name.strip(), adapter, base_url, payload.api_key or current["api_key"], payload.cookie or current["cookie"],
             payload.user_agent, payload.priority, payload.timeout_seconds, rss_url, payload.icon_url, int(payload.proxy),
             int(payload.render), payload.limit_interval, payload.limit_count, int(payload.enabled), int(payload.search_enabled), site_id),
        )
    site_icon_cache.pop(site_id, None)
    return {"id": site_id, "name": payload.name, "adapter": adapter, "enabled": payload.enabled, "search_enabled": payload.search_enabled}

@router.delete("/api/sites/{site_id}")
async def delete_site(site_id: int) -> dict[str, Any]:
    with connect() as conn:
        site = conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        if not site:
            raise HTTPException(404, "站点不存在")
        conn.execute("DELETE FROM pt_sites WHERE id=?", (site_id,))
    return {"deleted": site_id}

@router.post("/api/sites/{site_id}/test")
async def test_site(site_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone()
    if not row:
        raise HTTPException(404, "站点不存在")
    return await test_site_config(dict(row))

@router.post("/api/sites/test")
async def test_all_sites() -> dict[str, Any]:
    with connect() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM pt_sites WHERE enabled=1 ORDER BY priority,id").fetchall())
    semaphore = asyncio.Semaphore(4)
    async def probe(site: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await test_site_config(site)
    results = await asyncio.gather(*(probe(site) for site in rows))
    return {"total": len(results), "ok": sum(1 for result in results if result["ok"]), "results": results}

@router.post("/api/sites/sync-cookiecloud")
async def sync_sites_from_cookiecloud() -> dict[str, Any]:
    groups = cookie_groups(stored_cookiecloud_payload())
    updated: list[str] = []
    missing: list[str] = []
    with connect() as conn:
        rows = conn.execute("SELECT id,name,base_url FROM pt_sites ORDER BY id").fetchall()
        for row in rows:
            match = cookie_for_host(groups, urlparse(str(row["base_url"])).hostname or "")
            if not match:
                missing.append(str(row["name"]))
                continue
            conn.execute("UPDATE pt_sites SET cookie=?,migration_note=NULL WHERE id=?", (match[1], row["id"]))
            updated.append(str(row["name"]))
    return {
        "ok": bool(updated),
        "updated": len(updated),
        "sites": updated,
        "missing": missing,
        "message": f"已从 Chrome CookieCloud 更新 {len(updated)} 个站点 Cookie；UA 保留各站点现有配置",
    }

async def get_local_and_mp_site(site_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone()
    if not row:
        raise HTTPException(404, "站点不存在")
    local = dict(row)
    mp_sites, _, _ = await moviepilot_site_snapshot()
    mp_site = next((candidate for candidate in mp_sites if domain_match(str(candidate.get("domain") or candidate.get("url") or ""), local["base_url"])), None)
    if not mp_site:
        raise HTTPException(404, "MoviePilot 未找到对应站点")
    return local, mp_site

@router.post("/api/sites/{site_id}/sync-moviepilot")
async def sync_site_from_moviepilot(site_id: int) -> dict[str, Any]:
    local, mp_site = await get_local_and_mp_site(site_id)
    with connect() as conn:
        conn.execute("UPDATE pt_sites SET cookie=?,user_agent=?,proxy=?,render=?,migration_note=NULL WHERE id=?", (
            str(mp_site.get("cookie") or ""), str(mp_site.get("ua") or ""), int(bool(mp_site.get("proxy"))), int(bool(mp_site.get("render"))), local["id"],
        ))
    return {"ok": True, "name": local["name"], "message": "已从 MoviePilot 更新 Cookie 与 UA"}

@router.post("/api/sites/{site_id}/update-cookie-ua")
async def update_site_cookie_ua(site_id: int, payload: SiteCookiePayload) -> dict[str, Any]:
    local, mp_site = await get_local_and_mp_site(site_id)
    result = await MoviePilotClient().update_site_cookie(int(mp_site["id"]), payload.username, payload.password, payload.code)
    if not result.get("success", False):
        raise HTTPException(502, sanitize_sensitive_text(result.get("message") or "MoviePilot 更新 Cookie 失败"))
    await sync_site_from_moviepilot(site_id)
    return {"ok": True, "name": local["name"], "message": "MoviePilot 已更新并同步 Cookie 与 UA"}

@router.post("/api/sites/{site_id}/refresh-moviepilot-userdata")
async def refresh_site_moviepilot_userdata(site_id: int) -> dict[str, Any]:
    _, mp_site = await get_local_and_mp_site(site_id)
    result = await MoviePilotClient().refresh_site_user_data(int(mp_site["id"]))
    return {"ok": bool(result.get("success", True)), "message": sanitize_sensitive_text(result.get("message") or "已请求刷新用户数据")}

