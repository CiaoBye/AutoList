"""AutoList HTTP routes."""

from __future__ import annotations

import asyncio
import base64
import html
import sqlite3
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..clients import site_proxy
from ..config import APP_VERSION, settings
from ..database import connect
from ..queries import sites as queries
from ..logs import event_logger
from ..schemas import SitePayload
from ..services.cookiecloud import pull_cookiecloud
from ..sites import profile_for
from ..sites.engine import search_page_url
from ..sites.errors import SEARCH_CAPTCHA_MESSAGE
from ..services.sites import resolve_site_adapter, sync_sites_from_moviepilot, test_site_config
from ..security import safe_error, signed_media_url
from ..state import remember_site_icon, site_icon_cache
from ..util import raster_image_media_type, to_int
from ..outbound import safe_request, validate_remote_icon_url
from ..outbound import validated_base_url, validate_outbound_url
from ..responses import CookieCloudSynced, MoviePilotSitesSynced, Site, SiteCheck, SiteChecks, SiteCookieRefreshed, SiteSaved

router = APIRouter()
MAX_SITE_ICON_BYTES = 512 * 1024
SITE_ICON_ENDPOINT_VERSION = 2


def validated_rss_url(value: str) -> str:
    """Private feed query credentials stay server-side; retain outbound URL checks."""
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        return validate_outbound_url(normalized, label="RSS 地址")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def site_icon_fallback(name: str, base_url: str) -> bytes:
    """Build a deterministic monogram when a site's real favicon is unavailable.

    The fallback is deliberately generated locally: it never turns a failed
    favicon request into a blank image, and it does not use the URL scheme
    (``https://``) as the visible label.
    """
    candidate = "".join(str(name or "").strip().split())
    if not candidate:
        candidate = str(base_url or "?").strip()
    label = html.escape(candidate[:2].upper() or "?", quote=True)
    seed = sum((index + 1) * ord(char) for index, char in enumerate(candidate))
    palettes = (
        ("#e4ecff", "#3858a6"),
        ("#f4e9d9", "#8b5d18"),
        ("#e2f0ea", "#2f6f5d"),
        ("#eee7fb", "#6657a8"),
        ("#f7e3e1", "#a7443f"),
    )
    background, foreground = palettes[seed % len(palettes)]
    font_size = 20 if len(candidate) > 1 else 27
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
        f'viewBox="0 0 64 64" role="img" aria-label="{label}">'
        f'<rect width="64" height="64" rx="16" fill="{background}"/>'
        f'<text x="32" y="39" text-anchor="middle" font-family="Arial, sans-serif" '
        f'font-size="{font_size}" font-weight="700" letter-spacing="-1" fill="{foreground}">{label}</text>'
        "</svg>"
    ).encode()

def _site_form_values(
    payload: SitePayload, *, adapter: str, base_url: str, rss_url: str, api_key: str, cookie: str,
) -> dict[str, Any]:
    return {
        "name": payload.name.strip(), "adapter": adapter, "base_url": base_url, "api_key": api_key, "cookie": cookie,
        "user_agent": payload.user_agent, "priority": payload.priority, "timeout_seconds": payload.timeout_seconds,
        "rss_url": rss_url, "icon_url": payload.icon_url, "proxy": to_int(payload.proxy), "render": to_int(payload.render),
        "limit_interval": payload.limit_interval, "limit_count": payload.limit_count,
        "enabled": to_int(payload.enabled), "search_enabled": to_int(payload.search_enabled),
        "supplement_only": to_int(payload.supplement_only),
    }


@router.get("/api/sites", response_model=list[Site])
async def sites() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = queries.sites_with_recent_search_stats(conn)
    for item in rows:
        # 解析方式由站点地址与是否填写 API Key 决定（见 app/sites/profiles.py）。
        item["profile"] = profile_for(str(item.get("base_url") or ""), has_api_key=bool(str(item.get("api_key") or "").strip())).key
        total = to_int(item.pop("search_total") or 0)
        succeeded = to_int(item.pop("search_succeeded") or 0)
        item["local_stats"] = {
            "total": total,
            "succeeded": succeeded,
            "success_rate": round(succeeded / total * 100, 1) if total else None,
            "average_ms": to_int(item.pop("search_average_ms") or 0) if total else None,
            "result_count": to_int(item.pop("search_result_count") or 0),
            "last_attempt_at": item.pop("search_last_attempt_at"),
        }
        item["account_stats"] = {
            "uploaded": item.pop("account_uploaded"),
            "downloaded": item.pop("account_downloaded"),
            "ratio": item.pop("account_ratio"),
            "bonus": item.pop("account_bonus"),
            "seeding": item.pop("account_seeding"),
            "checked_at": item.pop("account_stats_checked_at"),
            "error": item.pop("account_stats_error"),
        }
        # 站点检测或寻片遇到搜索人机验证时，给出种子搜索页地址让用户去验证。
        needs_captcha = item.get("last_status") == "error" and SEARCH_CAPTCHA_MESSAGE in str(item.get("last_message") or "")
        item["verify_url"] = search_page_url(str(item.get("base_url") or "")) if needs_captcha else None
        item["api_key_configured"] = bool(item.get("api_key"))
        item["cookie_configured"] = bool(item.get("cookie"))
        item["user_agent_configured"] = bool(item.get("user_agent"))
        item["api_key"] = ""
        item["cookie"] = ""
        item["rss_url_configured"] = bool(item.get("rss_url"))
        item["rss_url"] = ""
        endpoint = signed_media_url(f"/api/sites/{item['id']}/icon")
        separator = "&" if "?" in endpoint else "?"
        item["icon_endpoint"] = f"{endpoint}{separator}v={SITE_ICON_ENDPOINT_VERSION}"
    return rows

@router.get("/api/sites/{site_id}/icon")
async def site_icon(site_id: int) -> Response:
    """Validate, proxy and cache a local/custom site favicon."""
    if site_id in site_icon_cache:
        content, media_type = site_icon_cache[site_id]
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
    with connect() as conn:
        row = queries.get_site(conn, site_id)
    if not row:
        raise HTTPException(404, "站点不存在")
    icon_value = str(row["icon_url"] or "")
    try:
        if icon_value.startswith("data:image/"):
            header, encoded = icon_value.split(",", 1)
            media_type = header.split(";", 1)[0].split(":", 1)[1]
            if len(encoded) > MAX_SITE_ICON_BYTES * 2:
                raise RuntimeError("站点图标过大")
            content = base64.b64decode(encoded, validate=True)
            if len(content) > MAX_SITE_ICON_BYTES:
                raise RuntimeError("站点图标过大")
        else:
            source = icon_value if icon_value.startswith(("http://", "https://")) else f"{str(row['base_url']).rstrip('/')}/favicon.ico"
            site = dict(row)
            source_host = str(urlparse(source).hostname or "").lower()
            base_host = str(urlparse(str(row["base_url"] or "")).hostname or "").lower()
            request_headers = {"User-Agent": str(row["user_agent"] or f"AutoList/{APP_VERSION}")}
            # Cookie is only useful for the configured site's own host. Never
            # send it to a custom icon CDN or another host.
            if source_host and source_host == base_host and row["cookie"]:
                request_headers["Cookie"] = str(row["cookie"])
            timeout_seconds = max(3.0, min(float(row["timeout_seconds"] or 12), 30.0))
            # 逐跳校验重定向目标：禁止自动跟随（否则内网地址已在请求后才被拦截）。
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                follow_redirects=False,
                proxy=site_proxy(site),
            ) as client:
                await validate_remote_icon_url(source, str(row["base_url"]))
                upstream = await safe_request(
                    client, "GET", source, headers=request_headers, label="图标地址",
                    max_response_bytes=MAX_SITE_ICON_BYTES, proxy_mode=bool(site_proxy(site)),
                )
                upstream.raise_for_status()
                if to_int(upstream.headers.get("content-length") or 0) > MAX_SITE_ICON_BYTES:
                    raise RuntimeError("图标文件过大")
                content = upstream.content
                if len(content) > MAX_SITE_ICON_BYTES:
                    raise RuntimeError("图标文件过大")
                media_type = upstream.headers.get("content-type", "image/x-icon").split(";", 1)[0]
        detected_media_type = raster_image_media_type(content)
        if not content or len(content) > MAX_SITE_ICON_BYTES or not detected_media_type:
            raise RuntimeError("图标内容无效")
        media_type = detected_media_type
        remember_site_icon(site_id, (content, media_type))
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        # A deterministic SVG fallback still gives every site a consistent visual anchor.
        fallback = site_icon_fallback(str(row["name"] if "name" in row.keys() else ""), str(row["base_url"] or ""))
        return Response(content=fallback, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

@router.get("/api/sites/{site_id}/health-history")
async def site_health_history(site_id: int, limit: int = 50) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 200))
    with connect() as conn:
        site = queries.get_site(conn, site_id)
        if not site:
            raise HTTPException(404, "站点不存在")
        items = queries.site_search_attempts(conn, site_id, safe_limit)
        summary = queries.site_search_summary(conn, site_id)
    total = to_int(summary["total"] or 0)
    summary["success_rate"] = round(to_int(summary["succeeded"] or 0) / total * 100, 1) if total else None
    return {"site": {"id": site["id"], "name": site["name"]}, "summary": summary, "items": items}

@router.post("/api/sites", response_model=SiteSaved)
async def add_site(payload: SitePayload) -> dict[str, Any]:
    base_url = validated_base_url(payload.base_url, "站点地址", True)
    rss_url = validated_rss_url(payload.rss_url)
    adapter = resolve_site_adapter(base_url, rss_url, cookie=payload.cookie or "")
    values = _site_form_values(
        payload, adapter=adapter, base_url=base_url, rss_url=rss_url, api_key=payload.api_key or "", cookie=payload.cookie or "",
    )
    try:
        with connect() as conn:
            site_id = queries.insert_site(conn, values, cookie_source="manual" if payload.cookie else None)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "站点名称已存在") from exc
    event_logger().info("site_added", extra={"site": payload.name.strip(), "detail": f"新增站点【{payload.name.strip()}】"})
    return {"id": site_id, "name": payload.name, "adapter": adapter, "enabled": payload.enabled, "search_enabled": payload.search_enabled}

@router.put("/api/sites/{site_id}", response_model=SiteSaved)
async def update_site(site_id: int, payload: SitePayload) -> dict[str, Any]:
    base_url = validated_base_url(payload.base_url, "站点地址", True)
    with connect() as conn:
        current = queries.get_site(conn, site_id)
        if not current:
            raise HTTPException(404, "站点不存在")
        rss_url = (
            "" if payload.clear_rss_url else
            (validated_rss_url(payload.rss_url) if payload.rss_url.strip() else str(current["rss_url"] or ""))
        )
        effective_cookie = "" if payload.clear_cookie else (payload.cookie or current["cookie"])
        adapter = resolve_site_adapter(base_url, rss_url, cookie=effective_cookie)
        cookie_changed = str(effective_cookie or "") != str(current["cookie"] or "")
        values = _site_form_values(
            payload, adapter=adapter, base_url=base_url, rss_url=rss_url,
            api_key="" if payload.clear_api_key else (payload.api_key or current["api_key"]), cookie=effective_cookie,
        )
        try:
            queries.update_site(conn, site_id, values)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "站点名称已存在") from exc
        if cookie_changed:
            queries.set_cookie_source(conn, site_id, "manual" if effective_cookie else None)
    site_icon_cache.pop(site_id, None)
    event_logger().info("site_updated", extra={"site": payload.name.strip(), "detail": f"更新站点【{payload.name.strip()}】配置"})
    return {"id": site_id, "name": payload.name, "adapter": adapter, "enabled": payload.enabled, "search_enabled": payload.search_enabled}

@router.delete("/api/sites/{site_id}")
async def delete_site(site_id: int) -> dict[str, Any]:
    with connect() as conn:
        site = queries.get_site(conn, site_id)
        if not site:
            raise HTTPException(404, "站点不存在")
        queries.delete_site(conn, site_id)
    site_icon_cache.pop(site_id, None)
    event_logger().info("site_deleted", extra={"site": str(site["name"]), "detail": f"删除站点【{site['name']}】"})
    return {"deleted": site_id}

@router.post("/api/sites/{site_id}/test", response_model=SiteCheck)
async def test_site(site_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = queries.get_site(conn, site_id)
    if not row:
        raise HTTPException(404, "站点不存在")
    return await test_site_config(dict(row))

@router.post("/api/sites/test", response_model=SiteChecks)
async def test_all_sites() -> dict[str, Any]:
    with connect() as conn:
        rows = queries.enabled_sites(conn)
    semaphore = asyncio.Semaphore(4)
    async def probe(site: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await test_site_config(site)
    results = await asyncio.gather(*(probe(site) for site in rows))
    ok_count = sum(1 for result in results if result["ok"])
    event_logger().info("sites_tested", extra={"detail": f"站点连通性测试完成：{ok_count}/{len(results)} 个可用", "total": len(results)})
    return {"total": len(results), "ok": ok_count, "results": results}

@router.post("/api/sites/sync-moviepilot", response_model=MoviePilotSitesSynced)
async def sync_sites_from_mp() -> dict[str, Any]:
    """Sync and import sites configured in MoviePilot into AutoList."""
    return await sync_sites_from_moviepilot()

@router.post("/api/sites/sync-cookiecloud", response_model=CookieCloudSynced)
async def sync_sites_from_cookiecloud() -> dict[str, Any]:
    with connect() as conn:
        site_count = queries.site_count(conn)
    import_note = ""
    if site_count == 0 and settings.mp_base_url and settings.mp_api_key:
        # 本地还没有任何站点时，先从 MoviePilot 导入站点，Cookie 才有可匹配的目标；结果写入提示。
        try:
            imported = await sync_sites_from_moviepilot()
            import_note = f"本地尚无站点，已先从 MoviePilot 导入 {imported.get('total', 0)} 个站点。"
        except Exception as exc:
            import_note = f"本地尚无站点，从 MoviePilot 导入失败：{safe_error(exc)}。"

    applied = await pull_cookiecloud("manual")
    updated, unchanged, missing = applied["updated"], applied["unchanged"], applied["missing"]
    unchanged_note = f"，{len(unchanged)} 个已是最新" if unchanged else ""
    message = f"{import_note}已从 CookieCloud 更新 {len(updated)} 个站点 Cookie{unchanged_note}；UA 保留各站点现有配置"
    event_logger().info(
        "cookiecloud_sync",
        extra={
            "detail": message,
            "total": len(updated),
        },
    )
    return {
        "ok": bool(updated or unchanged),
        "updated": len(updated),
        "unchanged": len(unchanged),
        "sites": updated,
        "missing": missing,
        "message": message,
    }

@router.post("/api/sites/{site_id}/refresh-cookie", response_model=SiteCookieRefreshed)
async def refresh_site_cookie(site_id: int) -> dict[str, Any]:
    """Refresh one local site's Cookie from the configured CookieCloud source."""
    with connect() as conn:
        row = queries.get_site(conn, site_id)
    if not row:
        raise HTTPException(404, "站点不存在")
    applied = await pull_cookiecloud("manual", site_id)
    if applied["unchanged"]:
        event_logger().info(
            "site_cookie_refreshed",
            extra={
                "site": str(row["name"]),
                "detail": f"【{row['name']}】的 Cookie 与 CookieCloud 一致，无需更新",
            },
        )
        return {
            "ok": True,
            "id": to_int(row["id"]),
            "name": str(row["name"]),
            "message": f"{row['name']} 的 Cookie 与 CookieCloud 一致，无需更新",
        }
    if not applied["updated"]:
        raise HTTPException(404, f"CookieCloud 中没有匹配 {row['name']} 域名的 Cookie")
    event_logger().info(
        "site_cookie_refreshed",
        extra={
            "site": str(row["name"]),
            "detail": f"已从 CookieCloud 刷新【{row['name']}】的 Cookie",
        },
    )
    return {
        "ok": True,
        "id": to_int(row["id"]),
        "name": str(row["name"]),
        "message": f"已从 CookieCloud 刷新 {row['name']} 的 Cookie；UA 保留现有配置",
    }
