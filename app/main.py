"""AutoList FastAPI entrypoint: middleware, routes, and compatibility re-exports."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import state
from .candidate_policy import merge_custom_rules, normalized_policy, release_group_catalog
from .clients import AIRecognitionClient, EmbyClient, MoviePilotClient, TMDBClient, TransmissionClient  # noqa: F401
from .config import access_token, access_token_required, load_runtime_settings, save_runtime_settings, settings
from .cookiecloud import cookie_for_host, cookie_groups
from .database import cleanup_old_data, config_values, connect, initialize, json_value, save_config
from .domain.titles import (
    candidate_identity,
    canonical_item_original_title,
    canonical_item_title,
    canonical_item_year,
    is_transmission_downloading,
    normalized_download_name,
    strict_torrent_matches_item,
    torrent_matches_item,
)
from .list_sources import PlaylistSourceFetcher
from .schemas import (
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
from .security import extract_access_token, safe_error, sanitize_sensitive_text, token_matches
from .services.automation import (
    add_notification,
    run_playlist_automation,
    run_recognition,
    start_playlist_automation,
    sync_playlist_incremental,
    sync_scheduler,
    update_automation_run,
    update_recognition_task,
)
from .services.cookiecloud_store import cookiecloud_file, stored_cookiecloud_payload
from .services.history import projected_download_history
from .services.imports import normalize_import_items, parse_json, parse_xlsx, resolve_import
from .services.library import library_details, library_state, run_library_scan, update_library_task
from .services.recognition import (
    analyze_candidate,
    extract_contexts,
    extract_pair,
    persist_tmdb_item,
    recognize_movie,
    select_tmdb_match,
    tmdb_item_values,
)
from .services.search import (
    build_search_queries,
    classify_search_error,
    create_followup_search_task,
    run_search,
    searchable_playlist_items,
    task_log,
    update_task,
)
from .services.sites import domain_match, moviepilot_site_snapshot, resolve_site_adapter, test_site_config
from .state import (
    AUTH_EXEMPT_PATHS,
    MAX_RUNNING_SEARCH_TASKS,
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
from .util import (
    MAX_COOKIECLOUD_BODY,
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

# Compatibility re-exports used by unit tests (`from app import main`).
from .clients import MTeamClient, NexusPHPClient, RSSClient, TorznabClient  # noqa: F401

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize()
    load_runtime_settings()
    cleanup_old_data()
    with connect() as conn:
        conn.execute(
            "UPDATE search_tasks SET status='interrupted', updated_at=? WHERE status IN ('queued','running')",
            (utc_now(),),
        )
        conn.execute(
            "UPDATE recognition_tasks SET status='interrupted', updated_at=? WHERE status IN ('queued','running')",
            (utc_now(),),
        )
        conn.execute(
            "UPDATE library_scan_tasks SET status='interrupted', updated_at=? WHERE status IN ('queued','running')",
            (utc_now(),),
        )
        conn.execute(
            "UPDATE automation_runs SET status='interrupted', updated_at=? WHERE status IN ('queued','running')",
            (utc_now(),),
        )
    state.scheduler_task = asyncio.create_task(sync_scheduler())
    try:
        yield
    finally:
        if state.scheduler_task:
            state.scheduler_task.cancel()
            await asyncio.gather(state.scheduler_task, return_exceptions=True)


app = FastAPI(title="AutoList", version="0.78", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "static"), name="assets")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "favicon.svg", media_type="image/svg+xml")


@app.middleware("http")
async def access_token_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    if not access_token_required():
        return await call_next(request)
    path = request.url.path
    if path in AUTH_EXEMPT_PATHS or path.startswith("/assets/") or path.startswith("/cookiecloud/"):
        return await call_next(request)
    provided = extract_access_token(request.headers.get("authorization"), request.headers.get("x-autolist-token"))
    if not token_matches(provided, access_token()):
        return JSONResponse({"detail": "需要有效的访问令牌"}, status_code=401)
    return await call_next(request)


def validated_base_url(value: str, label: str, required: bool) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        if required:
            raise HTTPException(422, f"{label}不能为空")
        return ""
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(422, f"{label}必须以 http:// 或 https:// 开头")
    if parsed.username or parsed.password:
        raise HTTPException(422, f"{label}不能包含用户名或密码")
    return normalized


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": app.version,
        "running_tasks": len(running_tasks) + len(running_automation_tasks),
        "access_token_required": access_token_required(),
    }


@app.get("/cookiecloud")
@app.get("/cookiecloud/")
async def cookiecloud_root() -> Response:
    return Response("AutoList CookieCloud API · /cookiecloud", media_type="text/plain")


@app.post("/cookiecloud/update")
async def cookiecloud_update(request: Request) -> dict[str, Any]:
    enforce_cookiecloud_rate_limit()
    try:
        content = decode_cookiecloud_body(await request.body(), request.headers.get("content-encoding", ""))
        payload = CookieCloudUploadPayload.model_validate_json(content)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"CookieCloud 上传数据无效：{safe_error(exc)}") from exc
    require_configured_cookiecloud_uuid(payload.uuid)
    path = cookiecloud_file(payload.uuid)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload.model_dump(), ensure_ascii=False), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)
    forwarded = False
    if settings.cookiecloud_forward_moviepilot and settings.mp_base_url:
        try:
            forward_url = f"{settings.mp_base_url.rstrip('/')}/cookiecloud/update"
            async with httpx.AsyncClient(timeout=settings.mp_timeout_seconds) as client:
                response = await client.post(forward_url, json=payload.model_dump())
                forwarded = response.is_success
        except Exception:
            forwarded = False
    return {"action": "done", "forwarded_moviepilot": forwarded}


@app.get("/cookiecloud/get/{uuid_value}")
async def cookiecloud_get(uuid_value: str) -> dict[str, Any]:
    require_configured_cookiecloud_uuid(uuid_value)
    path = cookiecloud_file(uuid_value)
    if not path.exists():
        raise HTTPException(404, "CookieCloud 数据不存在")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/cookiecloud/status")
async def cookiecloud_status() -> dict[str, Any]:
    configured = bool(settings.cookiecloud_key and settings.cookiecloud_password)
    path = cookiecloud_file(settings.cookiecloud_key) if settings.cookiecloud_key else None
    return {
        "configured": configured,
        "received": bool(path and path.exists()),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat() if path and path.exists() else None,
        "forward_moviepilot": settings.cookiecloud_forward_moviepilot,
        "endpoint": "/cookiecloud",
    }


@app.get("/api/connection")
async def connection() -> dict[str, Any]:
    async def check_one(name: str, client: Any) -> tuple[str, dict[str, Any]]:
        try:
            return name, await asyncio.wait_for(client.check(), timeout=6)
        except TimeoutError:
            return name, {"ok": False, "configured": True, "message": "连接检测超时"}
        except Exception as exc:
            return name, {"ok": False, "configured": True, "message": safe_error(exc)}
    checked = await asyncio.gather(*(
        check_one(name, client) for name, client in (
            ("tmdb", TMDBClient()), ("emby", EmbyClient()), ("transmission", TransmissionClient()), ("moviepilot", MoviePilotClient()),
        )
    ))
    results = dict(checked)
    # AutoList submits through MoviePilot; direct Transmission credentials are diagnostic only.
    required = (results["tmdb"], results["moviepilot"])
    return {"ok": all(item.get("ok") for item in required), "providers": results, "message": "核心服务正常" if all(item.get("ok") for item in required) else "核心服务需要配置"}


@app.get("/api/downloads")
async def downloads() -> list[dict[str, Any]]:
    try:
        return secret_free(await TransmissionClient().current_downloads())
    except Exception as exc:
        raise HTTPException(502, f"读取 Transmission 下载任务失败：{safe_error(exc)}") from exc


@app.get("/api/config")
async def get_config() -> dict[str, str]:
    return config_values()




@app.get("/api/settings")
async def get_runtime_settings() -> dict[str, Any]:
    return settings.public_values()


@app.put("/api/settings")
async def put_runtime_settings(payload: RuntimeSettingsPayload) -> dict[str, Any]:
    values = payload.model_dump()
    values["mp_base_url"] = validated_base_url(values["mp_base_url"], "MoviePilot 地址", False)
    values["emby_base_url"] = validated_base_url(values["emby_base_url"], "Emby 地址", False)
    values["ai_base_url"] = validated_base_url(values["ai_base_url"], "AI 地址", False)
    values["tr_base_url"] = validated_base_url(values["tr_base_url"], "Transmission 地址", False)
    if values.get("outbound_proxy_url"):
        values["outbound_proxy_url"] = validated_base_url(str(values["outbound_proxy_url"]), "代理地址", True)
    for key in ("mp_api_key", "emby_api_key", "tmdb_api_key", "mdblist_api_key", "cookiecloud_key", "cookiecloud_password", "ai_api_key", "tr_password"):
        if not values.get(key):
            values.pop(key, None)
    save_runtime_settings(values)
    return settings.public_values()


@app.post("/api/settings/test")
async def test_runtime_settings() -> dict[str, Any]:
    return (await connection())["providers"]


@app.get("/api/sites")
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


@app.get("/api/sites/{site_id}/icon")
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


@app.get("/api/sites/{site_id}/health-history")
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


@app.post("/api/sites")
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


@app.put("/api/sites/{site_id}")
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


@app.delete("/api/sites/{site_id}")
async def delete_site(site_id: int) -> dict[str, Any]:
    with connect() as conn:
        site = conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        if not site:
            raise HTTPException(404, "站点不存在")
        conn.execute("DELETE FROM pt_sites WHERE id=?", (site_id,))
    return {"deleted": site_id}


@app.post("/api/sites/{site_id}/test")
async def test_site(site_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone()
    if not row:
        raise HTTPException(404, "站点不存在")
    return await test_site_config(dict(row))


@app.post("/api/sites/test")
async def test_all_sites() -> dict[str, Any]:
    with connect() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM pt_sites WHERE enabled=1 ORDER BY priority,id").fetchall())
    semaphore = asyncio.Semaphore(4)
    async def probe(site: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await test_site_config(site)
    results = await asyncio.gather(*(probe(site) for site in rows))
    return {"total": len(results), "ok": sum(1 for result in results if result["ok"]), "results": results}


@app.post("/api/sites/sync-cookiecloud")
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


@app.post("/api/sites/{site_id}/sync-moviepilot")
async def sync_site_from_moviepilot(site_id: int) -> dict[str, Any]:
    local, mp_site = await get_local_and_mp_site(site_id)
    with connect() as conn:
        conn.execute("UPDATE pt_sites SET cookie=?,user_agent=?,proxy=?,render=?,migration_note=NULL WHERE id=?", (
            str(mp_site.get("cookie") or ""), str(mp_site.get("ua") or ""), int(bool(mp_site.get("proxy"))), int(bool(mp_site.get("render"))), local["id"],
        ))
    return {"ok": True, "name": local["name"], "message": "已从 MoviePilot 更新 Cookie 与 UA"}


@app.post("/api/sites/{site_id}/update-cookie-ua")
async def update_site_cookie_ua(site_id: int, payload: SiteCookiePayload) -> dict[str, Any]:
    local, mp_site = await get_local_and_mp_site(site_id)
    result = await MoviePilotClient().update_site_cookie(int(mp_site["id"]), payload.username, payload.password, payload.code)
    if not result.get("success", False):
        raise HTTPException(502, sanitize_sensitive_text(result.get("message") or "MoviePilot 更新 Cookie 失败"))
    await sync_site_from_moviepilot(site_id)
    return {"ok": True, "name": local["name"], "message": "MoviePilot 已更新并同步 Cookie 与 UA"}


@app.post("/api/sites/{site_id}/refresh-moviepilot-userdata")
async def refresh_site_moviepilot_userdata(site_id: int) -> dict[str, Any]:
    _, mp_site = await get_local_and_mp_site(site_id)
    result = await MoviePilotClient().refresh_site_user_data(int(mp_site["id"]))
    return {"ok": bool(result.get("success", True)), "message": sanitize_sensitive_text(result.get("message") or "已请求刷新用户数据")}


async def hydrate_recent_emby_posters(items: list[dict[str, Any]]) -> None:
    """Backfill legacy Emby references for the small home-page shelf without a full rescan."""
    if not items or not settings.emby_base_url or not settings.emby_api_key:
        return
    emby = EmbyClient()
    semaphore = asyncio.Semaphore(3)

    async def hydrate(item: dict[str, Any]) -> None:
        if item.get("emby_item_id") or item.get("library_state") not in {"in_library", "strm"}:
            return
        async with semaphore:
            _, emby_item_id, image_tag = await library_details(
                emby, item.get("tmdb_title") or item.get("chinese_title") or item["original_title"],
                item.get("tmdb_year") or item.get("year"), item.get("tmdb_id"),
                item.get("tmdb_imdb_id") or item.get("imdb_id"),
            )
        if not emby_item_id:
            return
        item["emby_item_id"] = emby_item_id
        item["emby_image_tag"] = image_tag
        with connect() as conn:
            conn.execute(
                "UPDATE playlist_items SET emby_item_id=?,emby_image_tag=? WHERE id=?",
                (emby_item_id, image_tag, item["id"]),
            )

    await asyncio.gather(*(hydrate(item) for item in items))


@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    with connect() as conn:
        playlist = conn.execute(
            """SELECT p.id, p.name, COUNT(i.id) AS item_count FROM playlists p
               LEFT JOIN playlist_items i ON i.playlist_id=p.id GROUP BY p.id ORDER BY p.position,p.id LIMIT 1"""
        ).fetchone()
        playlist_stats = conn.execute(
            """SELECT
                 SUM(CASE WHEN tmdb_id IS NOT NULL THEN 1 ELSE 0 END) AS recognized_count,
                 SUM(CASE WHEN library_state='in_library' THEN 1 ELSE 0 END) AS in_library_count
               FROM playlist_items WHERE playlist_id=?""",
            (playlist["id"],),
        ).fetchone() if playlist else None
        if playlist and settings.dashboard_random_posters:
            daily_seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
            recent_items = rows_to_dicts(conn.execute(
                """SELECT id,rank_no,imdb_id,tmdb_id,original_title,chinese_title,year,
                          tmdb_title,tmdb_original_title,tmdb_year,tmdb_imdb_id,
                          library_state,emby_item_id,emby_image_tag
                   FROM playlist_items WHERE playlist_id=? AND library_state='in_library'
                   ORDER BY ((id * (1103515245 + (? % 997))) & 2147483647),rank_no LIMIT 6""",
                (playlist["id"], daily_seed),
            ).fetchall())
        else:
            recent_items = rows_to_dicts(conn.execute(
                """SELECT id,rank_no,imdb_id,tmdb_id,original_title,chinese_title,year,
                          tmdb_title,tmdb_original_title,tmdb_year,tmdb_imdb_id,
                          library_state,emby_item_id,emby_image_tag
                   FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 6""",
                (playlist["id"],),
            ).fetchall()) if playlist else []
        latest_task = conn.execute("SELECT * FROM search_tasks ORDER BY id DESC LIMIT 1").fetchone()
        latest_candidates = conn.execute(
            "SELECT playlist_item_id,title,size,resource_key FROM candidates WHERE task_id=? AND eligibility='eligible'", (latest_task["id"],),
        ).fetchall() if latest_task else []
        latest_candidate_count = len({
            (row["playlist_item_id"], row["resource_key"] or resource_fingerprint(row["title"], row["size"]))
            for row in latest_candidates
        })
        cart_count = conn.execute("SELECT COUNT(*) FROM cart_items").fetchone()[0]
        history_count = conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0]
    await hydrate_recent_emby_posters(recent_items)
    for item in recent_items:
        item["poster_url"] = (
            f"/api/playlist-items/{item['id']}/poster?tag={item['emby_image_tag']}"
            if item.get("emby_item_id") and item.get("emby_image_tag") else None
        )
    return {
        "playlist_id": playlist["id"] if playlist else None,
        "playlist_name": playlist["name"] if playlist else None,
        "item_count": playlist["item_count"] if playlist else 0,
        "recognized_count": int(playlist_stats["recognized_count"] or 0) if playlist_stats else 0,
        "in_library_count": int(playlist_stats["in_library_count"] or 0) if playlist_stats else 0,
        "pending_count": max(0, int(playlist["item_count"] or 0) - int(playlist_stats["in_library_count"] or 0)) if playlist and playlist_stats else 0,
        "recent_items": recent_items,
        "cart_count": cart_count,
        "history_count": history_count,
        "latest_candidate_count": latest_candidate_count,
        "latest_task": dict(latest_task) if latest_task else None,
    }


@app.get("/api/playlist-items/{playlist_item_id}/poster")
async def playlist_item_poster(playlist_item_id: int, tag: str = "") -> Response:
    with connect() as conn:
        row = conn.execute(
            "SELECT emby_item_id,emby_image_tag FROM playlist_items WHERE id=?", (playlist_item_id,),
        ).fetchone()
    if not row or not row["emby_item_id"]:
        raise HTTPException(404, "影片没有可用的 Emby 海报")
    emby_item_id = str(row["emby_item_id"])
    if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", emby_item_id):
        raise HTTPException(404, "Emby 影片标识无效")
    image_tag = str(row["emby_image_tag"] or tag or "")
    cache_key = f"{emby_item_id}:{image_tag}"
    if cache_key in poster_cache:
        content, media_type = poster_cache[cache_key]
    else:
        try:
            content, _ = await EmbyClient().poster(emby_item_id)
        except httpx.HTTPStatusError as exc:
            status = 404 if exc.response.status_code == 404 else 502
            raise HTTPException(status, "Emby 海报读取失败") from exc
        except Exception as exc:
            raise HTTPException(502, f"Emby 海报读取失败：{safe_error(exc)}") from exc
        media_type = raster_image_media_type(content) or ""
        if not media_type or len(content) > 8 * 1024 * 1024:
            raise HTTPException(422, "Emby 返回的海报格式无效")
        if len(poster_cache) >= 64:
            poster_cache.pop(next(iter(poster_cache)))
        poster_cache[cache_key] = (content, media_type)
    return Response(
        content=content, media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@app.put("/api/config")
async def put_config(payload: ConfigPayload) -> dict[str, str]:
    raw = payload.model_dump(exclude_none=True)
    if "candidate_policy" in raw:
        try:
            raw["candidate_policy"] = normalized_policy(raw["candidate_policy"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, safe_error(exc)) from exc
        raw["candidate_limit"] = raw["candidate_policy"]["candidate_limit"]
    values = {
        key: json_value(value) if key in {"scoring_policy", "candidate_policy"} else str(value)
        for key, value in raw.items()
    }
    save_config(values)
    return config_values()


@app.post("/api/config/score-preview")
async def score_preview(payload: ScorePreviewPayload) -> dict[str, Any]:
    config = config_values()
    if payload.candidate_policy is not None:
        try:
            config["candidate_policy"] = json_value(normalized_policy(payload.candidate_policy))
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, safe_error(exc)) from exc
    return analyze_candidate(payload.title, 0, config, {"seeders": payload.seeders, "volume_factor": payload.volume_factor})


@app.get("/api/config/release-groups")
async def release_groups() -> dict[str, Any]:
    config = config_values()
    try:
        policy = normalized_policy(json.loads(config.get("candidate_policy") or "{}"))
    except (TypeError, json.JSONDecodeError):
        policy = normalized_policy({})
    return release_group_catalog(policy)


@app.post("/api/config/release-groups/import-moviepilot")
async def import_moviepilot_release_groups() -> dict[str, Any]:
    """Import MP custom groups once, then keep the merged vocabulary inside AutoList."""
    try:
        imported = await MoviePilotClient().custom_release_groups()
    except Exception as exc:
        raise HTTPException(502, f"MoviePilot 自定义制作组读取失败：{safe_error(exc)}") from exc
    config = config_values()
    try:
        policy = normalized_policy(json.loads(config.get("candidate_policy") or "{}"))
    except (TypeError, json.JSONDecodeError):
        policy = normalized_policy({})
    before = len(policy["custom_release_groups"])
    try:
        policy["custom_release_groups"] = merge_custom_rules([*policy["custom_release_groups"], *imported])
    except ValueError as exc:
        raise HTTPException(422, safe_error(exc)) from exc
    save_config({"candidate_policy": json_value(policy), "candidate_limit": str(policy["candidate_limit"])})
    catalog = release_group_catalog(policy)
    return {**catalog, "imported": len(policy["custom_release_groups"]) - before,
            "message": f"已合并 {len(policy['custom_release_groups']) - before} 条 MoviePilot 自定义制作组规则"}


@app.post("/api/playlists/import/preview")
async def preview_playlist_import(payload: ImportPayload) -> dict[str, Any]:
    try:
        source_name, items, source = await resolve_import(payload)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(422, f"片单获取失败：{safe_error(exc)}") from exc
    if not items:
        raise HTTPException(422, "没有获取到可导入的电影")
    return {"name": payload.name or source_name, "count": len(items), "source_type": source.get("source_type"), "sample": items[:8]}


@app.post("/api/playlists/import")
async def import_playlist(payload: ImportPayload) -> dict[str, Any]:
    try:
        source_name, items, source = await resolve_import(payload)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(422, f"片单获取失败：{safe_error(exc)}") from exc
    if not items:
        raise HTTPException(422, "没有可导入的影片")
    name = payload.name or source_name or f"片单 {datetime.now().strftime('%Y-%m-%d')}"
    with connect() as conn:
        position = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM playlists").fetchone()[0]
        cursor = conn.execute(
            "INSERT INTO playlists(name,position,source_type,source_url,source_name,last_synced_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (name, position, source.get("source_type"), source.get("source_url"), source.get("source_name"), utc_now(), utc_now()),
        )
        playlist_id = cursor.lastrowid
        conn.executemany(
            """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,tmdb_id)
               VALUES(:playlist_id,:rank_no,:imdb_id,:original_title,:year,:chinese_title,:tmdb_id)""",
            [{"playlist_id": playlist_id, "tmdb_id": None, **item} for item in items],
        )
    return {"id": playlist_id, "name": name, "count": len(items)}


@app.post("/api/playlists/{playlist_id}/refresh-source")
async def refresh_playlist_source(playlist_id: int) -> dict[str, Any]:
    with connect() as conn:
        playlist = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if not playlist:
            raise HTTPException(404, "片单不存在")
        if not playlist["source_url"]:
            raise HTTPException(422, "该片单不是通过网址导入的")
        active_tables = (
            ("search_tasks", "搜索"),
            ("recognition_tasks", "识别"),
            ("library_scan_tasks", "入库检查"),
        )
        for table, label in active_tables:
            if conn.execute(f"SELECT 1 FROM {table} WHERE playlist_id=? AND status IN ('queued','running')", (playlist_id,)).fetchone():  # nosec B608
                raise HTTPException(409, f"片单仍有{label}任务运行，请完成后再刷新")
    try:
        source = await PlaylistSourceFetcher().fetch(str(playlist["source_url"]), 10000)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(422, f"片单刷新失败：{safe_error(exc)}") from exc
    items = normalize_import_items(source.get("items", []))
    if not items:
        raise HTTPException(422, "来源没有返回可用电影，已保留现有片单")
    with connect() as conn:
        conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
        conn.executemany(
            """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,tmdb_id)
               VALUES(:playlist_id,:rank_no,:imdb_id,:original_title,:year,:chinese_title,:tmdb_id)""",
            [{"playlist_id": playlist_id, **item} for item in items],
        )
        conn.execute("UPDATE playlists SET source_name=?,last_synced_at=? WHERE id=?", (source.get("source_name"), utc_now(), playlist_id))
    return {"id": playlist_id, "count": len(items), "message": f"已从来源刷新 {len(items)} 部电影"}


@app.get("/api/playlists")
async def playlists() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.*, COUNT(i.id) AS item_count, SUM(CASE WHEN i.tmdb_id IS NOT NULL THEN 1 ELSE 0 END) AS recognized_count FROM playlists p
               LEFT JOIN playlist_items i ON i.playlist_id=p.id GROUP BY p.id ORDER BY p.position,p.id"""
        ).fetchall()
    return rows_to_dicts(rows)


@app.put("/api/playlists/{playlist_id}/automation")
async def configure_playlist_automation(playlist_id: int, payload: PlaylistAutomationPayload) -> dict[str, Any]:
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
            raise HTTPException(404, "片单不存在")
        conn.execute(
            "UPDATE playlists SET automation_enabled=?,automation_auto_cart=?,automation_batch_size=? WHERE id=?",
            (int(payload.enabled), int(payload.auto_cart), payload.batch_size, playlist_id),
        )
    return {"id": playlist_id, **payload.model_dump(), "auto_download": False}


@app.post("/api/playlists/{playlist_id}/automation/run")
async def run_playlist_automation_now(playlist_id: int) -> dict[str, Any]:
    return await start_playlist_automation(playlist_id)


@app.get("/api/automation-runs")
async def automation_runs(playlist_id: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 100))
    with connect() as conn:
        if playlist_id is None:
            rows = conn.execute("SELECT * FROM automation_runs ORDER BY id DESC LIMIT ?", (safe_limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM automation_runs WHERE playlist_id=? ORDER BY id DESC LIMIT ?", (playlist_id, safe_limit),
            ).fetchall()
    return rows_to_dicts(rows)


@app.put("/api/playlists/{playlist_id}/sync-settings")
async def configure_playlist_sync(playlist_id: int, payload: PlaylistSyncPayload) -> dict[str, Any]:
    with connect() as conn:
        playlist = conn.execute("SELECT source_url FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if not playlist:
            raise HTTPException(404, "片单不存在")
        if payload.enabled and not playlist["source_url"]:
            raise HTTPException(422, "只有网址导入的片单可以启用定时同步")
        next_sync = (datetime.now(timezone.utc) + timedelta(hours=payload.interval_hours)).isoformat() if payload.enabled else None
        conn.execute(
            "UPDATE playlists SET sync_enabled=?,sync_interval_hours=?,next_sync_at=? WHERE id=?",
            (int(payload.enabled), payload.interval_hours, next_sync, playlist_id),
        )
    return {"id": playlist_id, **payload.model_dump(), "next_sync_at": next_sync}


@app.post("/api/playlists/{playlist_id}/sync-now")
async def sync_playlist_now(playlist_id: int) -> dict[str, Any]:
    return await sync_playlist_incremental(playlist_id)


@app.get("/api/notifications")
async def notifications(limit: int = 30, unread_only: bool = False) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 100))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM notifications {'WHERE read=0' if unread_only else ''} ORDER BY id DESC LIMIT ?",  # nosec B608
            (safe_limit,),
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/notifications/read-all")
async def read_all_notifications() -> dict[str, Any]:
    with connect() as conn:
        count = conn.execute("UPDATE notifications SET read=1 WHERE read=0").rowcount
    return {"updated": count}


@app.get("/api/playlists/{playlist_id}/items")
async def playlist_items(
    playlist_id: int,
    page: int | None = None,
    page_size: int = 50,
    query: str = "",
    library_state: str = "all",
) -> Any:
    """Return a backwards-compatible full list or a bounded, filtered page."""
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
            raise HTTPException(404, "片单不存在")
        if page is None:
            rows = conn.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY rank_no", (playlist_id,)).fetchall()
            return rows_to_dicts(rows)
        safe_page = max(1, page)
        safe_page_size = max(1, min(page_size, 200))
        conditions = ["playlist_id=?"]
        params: list[Any] = [playlist_id]
        normalized_query = query.strip()
        if normalized_query:
            conditions.append("(original_title LIKE ? OR chinese_title LIKE ? OR tmdb_title LIKE ? OR tmdb_original_title LIKE ? OR imdb_id LIKE ? OR tmdb_imdb_id LIKE ?)")
            pattern = f"%{normalized_query}%"
            params.extend((pattern, pattern, pattern, pattern, pattern, pattern))
        if library_state == "not_downloaded":
            conditions.append("library_state!='in_library'")
        elif library_state in {"in_library", "unknown"}:
            conditions.append("library_state=?")
            params.append(library_state)
        elif library_state != "all":
            raise HTTPException(422, "无效的 Emby 状态筛选")
        where = " AND ".join(conditions)
        # SQL fragments in `conditions` are fixed literals; user values remain bound parameters.
        total = int(conn.execute(f"SELECT COUNT(*) FROM playlist_items WHERE {where}", params).fetchone()[0])  # nosec B608
        pages = max(1, math.ceil(total / safe_page_size))
        safe_page = min(safe_page, pages)
        rows = conn.execute(
            f"SELECT * FROM playlist_items WHERE {where} ORDER BY rank_no LIMIT ? OFFSET ?",  # nosec B608
            (*params, safe_page_size, (safe_page - 1) * safe_page_size),
        ).fetchall()
    return {
        "items": rows_to_dicts(rows), "total": total, "page": safe_page,
        "page_size": safe_page_size, "pages": pages,
    }


@app.post("/api/playlists/{playlist_id}/library-scan")
async def scan_playlist_library(playlist_id: int) -> dict[str, Any]:
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
            raise HTTPException(404, "片单不存在")
        active = conn.execute(
            "SELECT id FROM library_scan_tasks WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (playlist_id,),
        ).fetchone()
        if active:
            return {"id": active["id"], "message": "Emby 状态刷新正在进行"}
        total = conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (playlist_id,)).fetchone()[0]
        task_id = conn.execute(
            """INSERT INTO library_scan_tasks(playlist_id,status,total,created_at,updated_at)
               VALUES(?,?,?,?,?)""", (playlist_id, "queued", total, utc_now(), utc_now()),
        ).lastrowid
    running_library_tasks[task_id] = asyncio.create_task(run_library_scan(task_id))
    return {"id": task_id, "message": f"开始刷新 {total} 部影片的 Emby 状态"}


@app.get("/api/library-scan-tasks/{task_id}")
async def get_library_scan_task(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        task = conn.execute("SELECT * FROM library_scan_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "状态刷新任务不存在")
    return dict(task)


@app.put("/api/playlists/{playlist_id}")
async def update_playlist(playlist_id: int, payload: PlaylistUpdatePayload) -> dict[str, Any]:
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
            raise HTTPException(404, "片单不存在")
        conn.execute("UPDATE playlists SET name=? WHERE id=?", (payload.name.strip(), playlist_id))
    return {"id": playlist_id, "name": payload.name.strip()}


@app.post("/api/playlists/reorder")
async def reorder_playlists(payload: PlaylistOrderPayload) -> dict[str, Any]:
    with connect() as conn:
        existing = {row[0] for row in conn.execute("SELECT id FROM playlists")}
        if len(payload.ids) != len(existing) or len(set(payload.ids)) != len(payload.ids) or set(payload.ids) != existing:
            raise HTTPException(422, "排序列表必须包含全部片单")
        conn.executemany("UPDATE playlists SET position=? WHERE id=?", [(index, site_id) for index, site_id in enumerate(payload.ids, start=1)])
    return {"ids": payload.ids}


@app.post("/api/playlists/{playlist_id}/recognize")
async def recognize_playlist(playlist_id: int) -> dict[str, Any]:
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
            raise HTTPException(404, "片单不存在")
        total = conn.execute(
            """SELECT COUNT(*) FROM playlist_items
               WHERE playlist_id=? AND (tmdb_id IS NULL OR tmdb_title IS NULL OR tmdb_original_title IS NULL)""",
            (playlist_id,),
        ).fetchone()[0]
        if not total:
            return {"id": None, "status": "completed", "total": 0, "message": "片单已全部识别"}
        active = conn.execute(
            "SELECT id FROM recognition_tasks WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (playlist_id,),
        ).fetchone()
        if active:
            return {"id": active["id"], "status": "running", "total": total}
        task_id = conn.execute(
            "INSERT INTO recognition_tasks(playlist_id,status,total,created_at,updated_at) VALUES(?,?,?,?,?)",
            (playlist_id, "queued", total, utc_now(), utc_now()),
        ).lastrowid
    running_recognition_tasks[task_id] = asyncio.create_task(run_recognition(task_id))
    return {"id": task_id, "status": "queued", "total": total}


@app.get("/api/recognition-tasks/{task_id}")
async def recognition_task_status(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        task = conn.execute("SELECT * FROM recognition_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "识别任务不存在")
    return dict(task)


@app.delete("/api/playlists/{playlist_id}")
async def delete_playlist(playlist_id: int) -> dict[str, Any]:
    tasks_to_cancel: list[asyncio.Task[None]] = []
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "片单不存在")
        for table, registry in (
            ("search_tasks", running_tasks),
            ("recognition_tasks", running_recognition_tasks),
            ("library_scan_tasks", running_library_tasks),
        ):
            ids = conn.execute(
                f"SELECT id FROM {table} WHERE playlist_id=? AND status IN ('queued','running')", (playlist_id,),  # nosec B608
            ).fetchall()
            for row in ids:
                task = registry.get(int(row["id"]))
                if task and not task.done():
                    task.cancel()
                    tasks_to_cancel.append(task)
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    with connect() as conn:
        conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    return {"deleted": playlist_id}


@app.post("/api/search-tasks")
async def create_task(payload: TaskPayload) -> dict[str, Any]:
    enforce_search_task_capacity()
    if payload.scope == "range" and payload.range_end < payload.range_start:
        raise HTTPException(422, "结束序号不能小于起始序号")
    item_ids: list[int] = []
    if payload.scope == "pending":
        queue = await searchable_playlist_items(payload.playlist_id, payload.count)
        item_ids = [int(item["id"]) for item in queue["items"]]
        if not item_ids:
            raise HTTPException(422, "当前片单没有可搜索的未入库影片")
        range_start = min(int(item["rank_no"]) for item in queue["items"])
        range_end = max(int(item["rank_no"]) for item in queue["items"])
    else:
        range_start, range_end = payload.range_start, payload.range_end
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM playlist_items WHERE playlist_id=? AND rank_no BETWEEN ? AND ?",
            (payload.playlist_id, range_start, range_end),
        ).fetchone()[0]
        if not total:
            raise HTTPException(422, "所选范围没有影片")
        if not conn.execute("SELECT 1 FROM pt_sites WHERE enabled=1 AND search_enabled=1 LIMIT 1").fetchone():
            raise HTTPException(422, "请先在站点配置中选择至少一个参与搜索的站点")
        task_id = conn.execute(
            """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,trigger,item_ids_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (payload.playlist_id, range_start, range_end, "queued", len(item_ids) or total,
             "pending" if payload.scope == "pending" else "manual", json_value(item_ids) if item_ids else None,
             utc_now(), utc_now()),
        ).lastrowid
    running_tasks[task_id] = asyncio.create_task(run_search(task_id))
    return {"id": task_id, "status": "queued", "total": len(item_ids) or total, "scope": payload.scope}


@app.get("/api/playlists/{playlist_id}/searchable-items")
async def searchable_items(playlist_id: int, limit: int = 2000) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 10000))
    return await searchable_playlist_items(playlist_id, safe_limit)


@app.get("/api/search-tasks")
async def search_tasks(playlist_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 100))
    with connect() as conn:
        if playlist_id is None:
            rows = conn.execute("SELECT * FROM search_tasks ORDER BY id DESC LIMIT ?", (safe_limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM search_tasks WHERE playlist_id=? ORDER BY id DESC LIMIT ?",
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


@app.post("/api/search-tasks/{task_id}/cancel")
async def cancel_task(task_id: int) -> dict[str, Any]:
    task = running_tasks.get(task_id)
    if task:
        task.cancel()
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM search_tasks WHERE id=?", (task_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "搜索任务不存在")
    update_task(task_id, status="cancelled")
    return {"id": task_id, "status": "cancelled"}


@app.get("/api/search-tasks/{task_id}")
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


@app.get("/api/search-tasks/{task_id}/attempts")
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



@app.post("/api/search-tasks/{task_id}/retry")
async def retry_task(task_id: int) -> dict[str, Any]:
    enforce_search_task_capacity()
    new_id, total = create_followup_search_task(task_id, True)
    running_tasks[new_id] = asyncio.create_task(run_search(new_id))
    return {"id": new_id, "status": "queued", "total": total, "parent_task_id": task_id}


@app.post("/api/search-tasks/{task_id}/restart")
async def restart_task(task_id: int) -> dict[str, Any]:
    enforce_search_task_capacity()
    with connect() as conn:
        source = conn.execute("SELECT status FROM search_tasks WHERE id=?", (task_id,)).fetchone()
    if not source:
        raise HTTPException(404, "搜索任务不存在")
    if source["status"] in {"queued", "running"}:
        raise HTTPException(409, "任务仍在执行，无需重新启动")
    new_id, total = create_followup_search_task(task_id, False)
    running_tasks[new_id] = asyncio.create_task(run_search(new_id))
    return {"id": new_id, "status": "queued", "total": total, "parent_task_id": task_id}


@app.get("/api/search-tasks/{task_id}/logs")
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


@app.get("/api/candidates")
async def candidates(task_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        task = conn.execute("SELECT parent_task_id FROM search_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "搜索任务不存在")
        task_ids = [task_id]
        if task["parent_task_id"]:
            task_ids.append(int(task["parent_task_id"]))
        placeholders = ",".join("?" for _ in task_ids)
        rows = conn.execute(
            f"""SELECT c.*, p.rank_no, p.original_title, p.year, p.chinese_title,
                      p.tmdb_title,p.tmdb_original_title,p.tmdb_year,p.tmdb_imdb_id,
                      CASE WHEN cart.candidate_id IS NULL THEN 0 ELSE 1 END AS in_cart
               FROM candidates c JOIN playlist_items p ON p.id=c.playlist_item_id
               LEFT JOIN cart_items cart ON cart.candidate_id=c.id
               WHERE c.task_id IN ({placeholders}) ORDER BY p.rank_no, c.ranking""",  # nosec B608
            task_ids,
        ).fetchall()
        site_rows = conn.execute("SELECT name,priority,icon_url FROM pt_sites").fetchall()
    site_profiles = {str(row["name"]).lower(): dict(row) for row in site_rows}
    result = rows_to_dicts(rows)
    for item in result:
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
        options.sort(key=lambda item: (
            item["site_priority"], 0 if item["is_free"] else 1,
            item["volume_factor"],
            -int(item.get("seeders") or 0), int(item.get("ranking") or 0),
        ))
        primary = dict(options[0])
        primary["site_count"] = len(options)
        primary["site_options"] = [{
            "id": option["id"], "site_name": option.get("site_name"), "seeders": option.get("seeders"),
            "size": option.get("size"), "is_free": option["is_free"], "site_priority": option["site_priority"],
            "volume_factor": option["volume_factor"], "labels": option["metadata"].get("labels", []),
            "in_cart": option.get("in_cart", 0), "context_available": option["context_available"],
        } for option in options]
        factor_label = "免费" if primary["volume_factor"] == 0 else (f"下载 {int(primary['volume_factor'] * 100)}%" if primary["volume_factor"] < 1 else "普通")
        primary["site_selection_reason"] = (
            f"站点优先级 {primary['site_priority']} · {factor_label} · {int(primary.get('seeders') or 0)} 做种"
        )
        primary["in_cart"] = int(any(option.get("in_cart") for option in options))
        grouped.append(primary)
    grouped.sort(key=lambda item: (int(item["rank_no"] or 0), int(item.get("ranking") or 0)))
    return grouped


@app.post("/api/cart/items/{candidate_id}")
async def toggle_cart(candidate_id: str) -> dict[str, Any]:
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


@app.get("/api/cart")
async def cart() -> list[dict[str, Any]]:
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


@app.post("/api/cart/download")
async def download_cart() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.*, p.original_title FROM cart_items cart JOIN candidates c ON c.id=cart.candidate_id
               JOIN playlist_items p ON p.id=c.playlist_item_id ORDER BY cart.selected_at"""
        ).fetchall()
    if not rows:
        raise HTTPException(422, "下载列表为空")
    moviepilot, completed, needs_research, submitted_tasks, expired_items = MoviePilotClient(), 0, 0, [], []
    for candidate in rows:
        raw = raw_candidates.get(candidate["id"])
        if not raw:
            needs_research += 1
            expired_items.append({"candidate_id": candidate["id"], "title": candidate["original_title"]})
            message = "搜索上下文已失效，请重新搜索后加入下载列表"
            with connect() as conn:
                already_recorded = conn.execute(
                    "SELECT 1 FROM download_history WHERE candidate_id=? AND success=0 AND message=? LIMIT 1",
                    (candidate["id"], message),
                ).fetchone()
                if not already_recorded:
                    conn.execute(
                        """INSERT INTO download_history(
                               candidate_id,playlist_item_id,title,torrent_name,site_name,success,message,created_at
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        (candidate["id"], candidate["playlist_item_id"], candidate["original_title"], candidate["title"], candidate["site_name"], 0, message, utc_now()),
                    )
            continue
        try:
            if not raw.get("media"):
                raise RuntimeError("缺少媒体信息，无法应用 MoviePilot 分类规则")
            # 固定走 MoviePilot DownloadChain：它补全 TMDB 媒体信息、按 MP 分类目录选择路径，
            # 再交由 Transmission 写入 MOVIEPILOT 与站点标签，供 MP 后续整理。
            response = await moviepilot.download(raw["media"], raw["torrent"], downloader="Transmission")
            success = bool(response.get("success", True)) if isinstance(response, dict) else True
            message = response.get("message") or response.get("hash") if isinstance(response, dict) else None
            message = sanitize_sensitive_text(message) if message else None
            submission_hash = str(response.get("hash") or "").strip() or None if isinstance(response, dict) else None
            if isinstance(response, dict):
                submitted_tasks.append({"candidate_id": candidate["id"], "hash": response.get("hash"), "mode": "moviepilot"})
        except Exception as exc:
            success, message, submission_hash = False, safe_error(exc), None
        with connect() as conn:
            conn.execute(
                """INSERT INTO download_history(
                       candidate_id,playlist_item_id,title,torrent_name,site_name,submission_hash,success,message,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (candidate["id"], candidate["playlist_item_id"], candidate["original_title"], candidate["title"], candidate["site_name"], submission_hash, int(success), message, utc_now()),
            )
            if success:
                conn.execute("DELETE FROM cart_items WHERE candidate_id=?", (candidate["id"],))
                completed += 1
    if needs_research and completed == 0:
        raise HTTPException(409, f"下载列表中 {needs_research} 个资源的搜索上下文已失效，请重新搜索后加入下载列表")
    return {
        "submitted": completed, "needs_research": needs_research, "expired_items": expired_items,
        "mode": "moviepilot", "tasks": submitted_tasks,
    }


@app.get("/api/history")
async def history() -> list[dict[str, Any]]:
    return await projected_download_history()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")
