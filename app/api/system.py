"""AutoList HTTP routes."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ..candidate_policy import normalized_policy, release_group_catalog
from ..clients import EmbyClient, FanartClient, MoviePilotClient, TMDBClient, TransmissionClient
from ..config import (
    APP_VERSION,
    access_token_required,
    access_token_strength,
    access_token_strength_enforced,
    public_endpoint_url,
    save_runtime_settings,
    settings,
)
from ..cookiecloud import cookie_groups, decrypt_cookiecloud
from ..database import config_values, json_value, save_config
from ..logs import event_logger
from ..schemas import (
    COOKIECLOUD_KEY_PATTERN,
    ConfigPayload,
    CookieCloudUploadPayload,
    RuntimeSettingsPayload,
    ScorePreviewPayload,
)
from ..security import safe_error
from ..services.cookiecloud_store import cookiecloud_file
from ..services.recognition import analyze_candidate
from ..services.sites import apply_cookie_groups
from .. import state
from ..state import (
    enforce_cookiecloud_anonymous_rate_limit,
    enforce_cookiecloud_get_rate_limit,
    enforce_cookiecloud_rate_limit,
    require_configured_cookiecloud_uuid,
    scheduler_health,
)
from ..util import decode_cookiecloud_body, read_request_body_limited, validated_base_url

router = APIRouter()

# Populated by app.main after router registration.
@router.get("/api/health")
async def health() -> dict[str, Any]:
    scheduler = scheduler_health()
    return {
        "ok": True,
        "version": APP_VERSION,
        "access_token_required": access_token_required(),
        "access_token_strength": access_token_strength(),
        "access_token_strength_enforced": access_token_strength_enforced(),
        "scheduler": scheduler,
        "scheduler_ok": scheduler["ok"],
    }

@router.get("/cookiecloud")
@router.get("/cookiecloud/")
@router.get("/cookiecloud//")
async def cookiecloud_root() -> Response:
    return Response("AutoList CookieCloud API · /cookiecloud", media_type="text/plain")

@router.post("/cookiecloud/update")
@router.post("/cookiecloud/update/")
@router.post("/cookiecloud//update")
@router.post("/update")
@router.post("/update/")
@router.post("//update")
async def cookiecloud_update(request: Request) -> dict[str, Any]:
    anonymous_rejection = enforce_cookiecloud_anonymous_rate_limit()
    if anonymous_rejection is not None:
        raise HTTPException(429, anonymous_rejection)
    try:
        raw_content = await read_request_body_limited(request)
        content = decode_cookiecloud_body(raw_content, request.headers.get("content-encoding", ""))
        payload = CookieCloudUploadPayload.model_validate_json(content)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"CookieCloud 上传数据无效：{safe_error(exc)}") from exc
    uuid_rejection = require_configured_cookiecloud_uuid(payload.uuid)
    if uuid_rejection is not None:
        raise HTTPException(uuid_rejection[0], uuid_rejection[1])
    # 限流在 uuid 校验之后：未认证垃圾请求不得消耗合法同步预算（审计 2-4）。
    rate_rejection = enforce_cookiecloud_rate_limit()
    if rate_rejection is not None:
        raise HTTPException(429, rate_rejection)
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
    applied = apply_cookie_groups(groups, origin="push")
    event_logger().info(
        "cookiecloud_uploaded",
        extra={
            "detail": f"收到 Chrome 扩展推送，已自动更新 {len(applied['updated'])} 个站点 Cookie",
            "total": len(applied["updated"]),
        },
    )
    return {
        "action": "done",
        "updated_sites": len(applied["updated"]),
        "missing_sites": applied["missing"],
    }

@router.get("/cookiecloud/get/{uuid_value}")
@router.get("/cookiecloud/get/{uuid_value}/")
@router.get("/cookiecloud//get/{uuid_value}")
@router.get("/get/{uuid_value}")
@router.get("/get/{uuid_value}/")
@router.get("//get/{uuid_value}")
async def cookiecloud_get(uuid_value: str) -> dict[str, Any]:
    read_rejection = enforce_cookiecloud_get_rate_limit()
    if read_rejection is not None:
        raise HTTPException(429, read_rejection)
    uuid_rejection = require_configured_cookiecloud_uuid(uuid_value)
    if uuid_rejection is not None:
        raise HTTPException(uuid_rejection[0], uuid_rejection[1])
    path = cookiecloud_file(uuid_value)
    if not path.exists():
        raise HTTPException(404, "CookieCloud 数据不存在")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "CookieCloud 数据文件无法解析") from exc
    return data

@router.get("/api/cookiecloud/status")
async def cookiecloud_status() -> dict[str, Any]:
    key = (settings.cookiecloud_key or "").strip()
    key_valid = bool(re.fullmatch(COOKIECLOUD_KEY_PATTERN, key)) if key else False
    configured = bool(key_valid and settings.cookiecloud_password)
    try:
        path = cookiecloud_file(key) if key_valid else None
    except HTTPException:
        path = None
    return {
        "configured": configured,
        "key_valid": key_valid,
        "url_configured": bool(settings.cookiecloud_url),
        "url": public_endpoint_url(settings.cookiecloud_url),
        "received": bool(path and path.exists()),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat() if path and path.exists() else None,
        "endpoint": "/cookiecloud",
        # 最近一次整体同步：origin 为 pull（定时拉取）/ push（插件推送）/ manual（手动同步）。
        "last_sync": dict(state.last_cookie_sync) or None,
    }

_connection_cache: tuple[float, dict[str, Any]] | None = None
CONNECTION_CACHE_TTL_SECONDS = 30

RUNTIME_SETTING_CLEAR_FIELDS = {
    "mp_base_url": "clear_mp_base_url",
    "mp_api_key": "clear_mp_api_key",
    "emby_base_url": "clear_emby_base_url",
    "emby_api_key": "clear_emby_api_key",
    "tmdb_api_key": "clear_tmdb_api_key",
    "fanart_api_key": "clear_fanart_api_key",
    "mdblist_api_key": "clear_mdblist_api_key",
    "cookiecloud_url": "clear_cookiecloud_url",
    "cookiecloud_key": "clear_cookiecloud_key",
    "cookiecloud_password": "clear_cookiecloud_password",
    "outbound_proxy_url": "clear_outbound_proxy_url",
    "ai_base_url": "clear_ai_base_url",
    "ai_api_key": "clear_ai_api_key",
    "ai_model": "clear_ai_model",
    "tr_base_url": "clear_tr_base_url",
    "tr_username": "clear_tr_username",
    "tr_password": "clear_tr_password",
}


def _runtime_setting_clear_values(raw_payload: dict[str, Any]) -> dict[str, str]:
    """Parse explicit clear markers without weakening partial-update semantics.

    RuntimeSettingsPayload intentionally keeps secret fields optional and
    redacted.  The clear protocol is therefore read from the raw JSON body so
    clients can send ``clear_tmdb_api_key: true`` without ever echoing the
    existing secret.  A nested ``clear`` object is accepted as a convenience
    for future clients; both forms are restricted to the same allowlist.
    """
    nested = raw_payload.get("clear", {})
    if nested is None:
        nested = {}
    if not isinstance(nested, dict):
        raise HTTPException(422, "settings.clear 必须是对象")
    cleared: dict[str, str] = {}
    for field, marker in RUNTIME_SETTING_CLEAR_FIELDS.items():
        values = []
        if marker in raw_payload:
            values.append(raw_payload[marker])
        if field in nested:
            values.append(nested[field])
        if marker in nested:
            values.append(nested[marker])
        if not values:
            continue
        if any(value is not True for value in values):
            raise HTTPException(422, f"{marker} 必须是 true 或省略")
        cleared[field] = ""
    unknown = set(nested) - set(RUNTIME_SETTING_CLEAR_FIELDS) - set(RUNTIME_SETTING_CLEAR_FIELDS.values())
    if unknown:
        raise HTTPException(422, f"不支持清除设置：{', '.join(sorted(str(item) for item in unknown))}")
    return cleared


@router.get("/api/connection")
async def connection(force_refresh: bool = False) -> dict[str, Any]:
    global _connection_cache
    if not force_refresh and _connection_cache is not None and time.monotonic() - _connection_cache[0] < CONNECTION_CACHE_TTL_SECONDS:
        return _connection_cache[1]
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
    payload = {"ok": all(item.get("ok") for item in required), "providers": results, "message": "核心服务正常" if all(item.get("ok") for item in required) else "核心服务需要配置", "version": APP_VERSION}
    _connection_cache = (time.monotonic(), payload)
    return payload


@router.get("/api/config")
async def get_config() -> dict[str, str]:
    return config_values()

@router.get("/api/settings")
async def get_runtime_settings() -> dict[str, Any]:
    return settings.public_values()

@router.put("/api/settings")
async def put_runtime_settings(payload: RuntimeSettingsPayload, request: Request) -> dict[str, Any]:
    values = payload.model_dump(exclude_unset=True)
    try:
        raw_payload = await request.json()
    except Exception as exc:
        raise HTTPException(422, "设置请求体无效") from exc
    if not isinstance(raw_payload, dict):
        raise HTTPException(422, "设置请求体必须是对象")
    values.update(_runtime_setting_clear_values(raw_payload))
    for key, label in (
        ("mp_base_url", "MoviePilot 地址"),
        ("emby_base_url", "Emby 地址"),
        ("ai_base_url", "AI 地址"),
        ("tr_base_url", "Transmission 地址"),
        ("cookiecloud_url", "CookieCloud 服务器地址"),
    ):
        # null 表示“保留原值”（可选地址字段），不能在校验时被转换成空串而误清除。
        if key in values and values[key] is not None:
            values[key] = validated_base_url(values[key], label, False, allow_private=True)
    if values.get("outbound_proxy_url"):
        values["outbound_proxy_url"] = validated_base_url(str(values["outbound_proxy_url"]), "代理地址", True, allow_private=True)
    for key in ("mp_api_key", "emby_api_key", "tmdb_api_key", "fanart_api_key", "mdblist_api_key", "cookiecloud_url", "cookiecloud_key", "cookiecloud_password", "ai_api_key", "tr_username", "tr_password"):
        if values.get(key) is None:
            values.pop(key, None)
        # API 客户端显式发送空字符串时清除；设置页留空字段则发送 null 并保留原值。
        elif str(values[key]).strip() == "":
            values[key] = ""
    save_runtime_settings(values)
    event_logger().info("settings_saved", extra={"detail": "系统配置已保存"})
    global _connection_cache
    _connection_cache = None
    return settings.public_values()

@router.post("/api/settings/test")
async def test_runtime_settings(provider: Literal["tmdb", "fanart", "emby", "transmission", "moviepilot", "cookiecloud"] | None = None) -> dict[str, Any]:
    if provider == "cookiecloud":
        if not settings.cookiecloud_key or not settings.cookiecloud_password:
            return {"cookiecloud": {"ok": False, "configured": False, "message": "未配置 CookieCloud 用户 KEY 或端对端密码"}}
        if settings.cookiecloud_url:
            from ..services.cookiecloud_store import fetch_remote_cookiecloud
            try:
                await asyncio.wait_for(
                    fetch_remote_cookiecloud(settings.cookiecloud_url, settings.cookiecloud_key, settings.cookiecloud_password),
                    timeout=10,
                )
                return {"cookiecloud": {"ok": True, "configured": True, "message": "CookieCloud 连接并解密成功"}}
            except Exception as exc:
                return {"cookiecloud": {"ok": False, "configured": True, "message": safe_error(exc)}}
        path = cookiecloud_file(settings.cookiecloud_key)
        if path.exists():
            return {"cookiecloud": {"ok": True, "configured": True, "message": "已收到推送数据"}}
        return {"cookiecloud": {"ok": False, "configured": False, "message": "未配置服务器地址或未收到推送"}}
    async def check(client: Any) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(client.check(), timeout=6)
        except TimeoutError:
            return {"ok": False, "configured": True, "message": "连接检测超时"}
        except Exception as exc:
            return {"ok": False, "configured": True, "message": safe_error(exc)}
    if provider is not None:
        client = {
            "tmdb": TMDBClient, "fanart": FanartClient, "emby": EmbyClient,
            "transmission": TransmissionClient, "moviepilot": MoviePilotClient,
        }[provider]()
        return {provider: await check(client)}
    # Fanart 只提供海报，不计入顶栏“服务正常”统计，但“检测全部”仍一并检测。
    providers = dict((await connection(force_refresh=True))["providers"])
    providers["fanart"] = await check(FanartClient())
    return providers

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

UI_INDEX = Path(__file__).resolve().parent.parent / "static" / "ui" / "index.html"


@router.get("/")
async def index() -> Response:
    """电影藏馆界面入口；前端由 frontend/ 构建到 app/static/ui。"""
    if not UI_INDEX.is_file():
        return Response(
            "界面尚未构建：请在 frontend/ 目录执行 npm ci && npm run build，或使用包含前端构建阶段的镜像。",
            status_code=503, media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
    return FileResponse(UI_INDEX, headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(
        Path(__file__).resolve().parent.parent / "static" / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
