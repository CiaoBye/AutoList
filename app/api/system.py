"""AutoList HTTP routes."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ..candidate_policy import normalized_policy, release_group_catalog
from ..clients import EmbyClient, MoviePilotClient, TMDBClient, TransmissionClient
from ..config import access_token_required, load_runtime_settings, save_runtime_settings, settings
from ..cookiecloud import cookie_groups, decrypt_cookiecloud
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
from ..services.history import projected_download_history
from ..services.imports import normalize_import_items, resolve_import
from ..services.library import run_library_scan
from ..services.recognition import analyze_candidate, persist_tmdb_item, recognize_movie
from ..services.search import (
    create_followup_search_task,
    run_search,
    searchable_playlist_items,
)
from ..services.sites import apply_cookie_groups, resolve_site_adapter, test_site_config
from ..state import (
    enforce_cookiecloud_rate_limit,
    enforce_search_task_capacity,
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


def validated_base_url(value: str, label: str, required: bool) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        if required:
            raise HTTPException(422, f"{label}不能为空")
        return ""
    try:
        parsed = urlparse(normalized)
        hostname = parsed.hostname
    except ValueError as exc:
        raise HTTPException(422, f"{label}地址格式无效") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise HTTPException(422, f"{label}必须以 http:// 或 https:// 开头")
    if parsed.username or parsed.password:
        raise HTTPException(422, f"{label}不能包含用户名或密码")
    sensitive_query = re.compile(
        r"^(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|passkey|password|passwd|secret|cookie|authorization|key)$",
        re.I,
    )
    if any(sensitive_query.fullmatch(key.strip()) for key, _value in parse_qsl(parsed.query, keep_blank_values=True)):
        raise HTTPException(422, f"{label}不能在查询参数中包含密钥或密码")
    return normalized



# Populated by app.main after router registration.
APP_VERSION = "0.85"

@router.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": APP_VERSION,
        "running_tasks": len(running_tasks) + len(running_automation_tasks),
        "access_token_required": access_token_required(),
    }

@router.get("/cookiecloud")
@router.get("/cookiecloud/")
async def cookiecloud_root() -> Response:
    return Response("AutoList CookieCloud API · /cookiecloud", media_type="text/plain")

@router.post("/cookiecloud/update")
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
    if not settings.cookiecloud_password:
        raise HTTPException(422, "请先在 AutoList 设置中配置 CookieCloud 端对端加密密码")
    try:
        decrypted = decrypt_cookiecloud(
            payload.uuid,
            settings.cookiecloud_password,
            payload.encrypted,
            payload.crypto_type,
        )
        groups = cookie_groups(decrypted)
    except Exception as exc:
        raise HTTPException(422, f"CookieCloud 解密失败：{safe_error(exc)}") from exc
    path = cookiecloud_file(payload.uuid)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(json.dumps(payload.model_dump(), ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    path.chmod(0o600)
    applied = apply_cookie_groups(groups)
    return {
        "action": "done",
        "updated_sites": len(applied["updated"]),
        "missing_sites": applied["missing"],
    }

@router.get("/cookiecloud/get/{uuid_value}")
async def cookiecloud_get(uuid_value: str) -> dict[str, Any]:
    require_configured_cookiecloud_uuid(uuid_value)
    path = cookiecloud_file(uuid_value)
    if not path.exists():
        raise HTTPException(404, "CookieCloud 数据不存在")
    return json.loads(path.read_text(encoding="utf-8"))

@router.get("/api/cookiecloud/status")
async def cookiecloud_status() -> dict[str, Any]:
    key = (settings.cookiecloud_key or "").strip()
    key_valid = bool(re.fullmatch(r"[A-Za-z0-9_-]{5,128}", key)) if key else False
    configured = bool(key_valid and settings.cookiecloud_password)
    try:
        path = cookiecloud_file(key) if key_valid else None
    except HTTPException:
        path = None
    return {
        "configured": configured,
        "key_valid": key_valid,
        "received": bool(path and path.exists()),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat() if path and path.exists() else None,
        "endpoint": "/cookiecloud",
    }

@router.get("/api/connection")
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

@router.get("/api/downloads")
async def downloads() -> list[dict[str, Any]]:
    try:
        return secret_free(await TransmissionClient().current_downloads())
    except Exception as exc:
        raise HTTPException(502, f"读取 Transmission 下载任务失败：{safe_error(exc)}") from exc

@router.get("/api/config")
async def get_config() -> dict[str, str]:
    return config_values()

@router.get("/api/settings")
async def get_runtime_settings() -> dict[str, Any]:
    return settings.public_values()

@router.put("/api/settings")
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

@router.post("/api/settings/test")
async def test_runtime_settings() -> dict[str, Any]:
    return (await connection())["providers"]

@router.put("/api/config")
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

@router.post("/api/config/score-preview")
async def score_preview(payload: ScorePreviewPayload) -> dict[str, Any]:
    config = config_values()
    if payload.candidate_policy is not None:
        try:
            config["candidate_policy"] = json_value(normalized_policy(payload.candidate_policy))
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, safe_error(exc)) from exc
    return analyze_candidate(payload.title, 0, config, {"seeders": payload.seeders, "volume_factor": payload.volume_factor})

@router.get("/api/config/release-groups")
async def release_groups() -> dict[str, Any]:
    config = config_values()
    try:
        policy = normalized_policy(json.loads(config.get("candidate_policy") or "{}"))
    except (TypeError, json.JSONDecodeError):
        policy = normalized_policy({})
    return release_group_catalog(policy)

@router.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parent.parent / "static" / "index.html")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(
        Path(__file__).resolve().parent.parent / "static" / "favicon.svg",
        media_type="image/svg+xml",
    )
