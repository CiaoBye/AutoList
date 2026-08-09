"""AutoList HTTP routes."""

from __future__ import annotations

import asyncio
import base64
import sqlite3
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..cookiecloud import cookie_groups
from ..database import connect
from ..schemas import SitePayload
from ..services.cookiecloud_store import stored_cookiecloud_payload
from ..services.sites import apply_cookie_groups, resolve_site_adapter, test_site_config
from ..state import remember_site_icon, site_icon_cache
from ..util import raster_image_media_type, rows_to_dicts, safe_request, utc_now, validate_remote_icon_url
from .system import validated_base_url

router = APIRouter()
MAX_SITE_ICON_BYTES = 512 * 1024

@router.get("/api/sites")
async def sites() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = rows_to_dicts(conn.execute(
            """SELECT s.*,
                      COUNT(a.id) AS search_total,
                      SUM(CASE WHEN a.status='success' THEN 1 ELSE 0 END) AS search_succeeded,
                      CAST(AVG(a.duration_ms) AS INTEGER) AS search_average_ms,
                      SUM(a.result_count) AS search_result_count,
                      MAX(a.finished_at) AS search_last_attempt_at
               FROM pt_sites s
               LEFT JOIN search_attempts a ON a.site_id=s.id
               GROUP BY s.id
               ORDER BY s.id"""
        ).fetchall())
    for item in rows:
        total = int(item.pop("search_total") or 0)
        succeeded = int(item.pop("search_succeeded") or 0)
        item["local_stats"] = {
            "total": total,
            "succeeded": succeeded,
            "success_rate": round(succeeded / total * 100, 1) if total else None,
            "average_ms": int(item.pop("search_average_ms") or 0) if total else None,
            "result_count": int(item.pop("search_result_count") or 0),
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
        item["api_key_configured"] = bool(item.get("api_key"))
        item["cookie_configured"] = bool(item.get("cookie"))
        item["user_agent_configured"] = bool(item.get("user_agent"))
        item["api_key"] = ""
        item["cookie"] = ""
        item["rss_url_configured"] = bool(item.get("rss_url"))
        item["rss_url"] = ""
        item["icon_endpoint"] = f"/api/sites/{item['id']}/icon"
    return rows

@router.get("/api/sites/{site_id}/icon")
async def site_icon(site_id: int) -> Response:
    """Validate, proxy and cache a local/custom site favicon."""
    if site_id in site_icon_cache:
        content, media_type = site_icon_cache[site_id]
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
    with connect() as conn:
        row = conn.execute("SELECT id,base_url,icon_url FROM pt_sites WHERE id=?", (site_id,)).fetchone()
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
            # 逐跳校验重定向目标：禁止自动跟随（否则内网地址已在请求后才被拦截）。
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
                await validate_remote_icon_url(source, str(row["base_url"]))
                upstream = await safe_request(client, "GET", source, label="图标地址")
                upstream.raise_for_status()
                if int(upstream.headers.get("content-length") or 0) > MAX_SITE_ICON_BYTES:
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
        rss_url = (
            "" if payload.clear_rss_url else
            (validated_base_url(payload.rss_url, "RSS 地址", False) if payload.rss_url.strip() else str(current["rss_url"] or ""))
        )
        adapter = resolve_site_adapter(base_url, rss_url)
        conn.execute(
            """UPDATE pt_sites SET name=?,adapter=?,base_url=?,api_key=?,cookie=?,user_agent=?,priority=?,timeout_seconds=?,rss_url=?,icon_url=?,proxy=?,render=?,limit_interval=?,limit_count=?,enabled=?,search_enabled=?,migration_note=NULL WHERE id=?""",
            (payload.name.strip(), adapter, base_url,
             "" if payload.clear_api_key else (payload.api_key or current["api_key"]),
             "" if payload.clear_cookie else (payload.cookie or current["cookie"]),
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
    applied = apply_cookie_groups(groups)
    updated, missing = applied["updated"], applied["missing"]
    return {
        "ok": bool(updated),
        "updated": len(updated),
        "sites": updated,
        "missing": missing,
        "message": f"已从 Chrome CookieCloud 更新 {len(updated)} 个站点 Cookie；UA 保留各站点现有配置",
    }

@router.post("/api/sites/{site_id}/refresh-cookie")
async def refresh_site_cookie(site_id: int) -> dict[str, Any]:
    """Refresh one local site's Cookie from AutoList's own CookieCloud store."""
    with connect() as conn:
        row = conn.execute("SELECT id,name,base_url FROM pt_sites WHERE id=?", (site_id,)).fetchone()
    if not row:
        raise HTTPException(404, "站点不存在")
    applied = apply_cookie_groups(cookie_groups(stored_cookiecloud_payload()), site_id)
    if not applied["updated"]:
        raise HTTPException(404, f"CookieCloud 中没有匹配 {row['name']} 域名的 Cookie")
    return {
        "ok": True,
        "id": int(row["id"]),
        "name": str(row["name"]),
        "message": f"已从 AutoList CookieCloud 刷新 {row['name']} 的 Cookie；UA 保留现有配置",
    }
