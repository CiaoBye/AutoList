"""PT site adapter selection and connection helpers."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from ..clients import MTeamClient, NexusPHPClient, RSSClient, TorznabClient
from ..cookiecloud import cookie_for_host
from .cookiecloud import with_cookie_refresh
from .. import state
from ..database import connect
from ..security import safe_error, sanitize_sensitive_text
from ..util import utc_now
from ..outbound import validate_outbound_url

SITE_STATS_SUCCESS_TTL = timedelta(hours=6)
SITE_STATS_FAILURE_TTL = timedelta(hours=1)
SLOW_SITE_THRESHOLD_MS = 3000


def friendly_site_error(exc: Exception) -> str:
    """Translate transport errors into short Chinese diagnostics without raw httpx text."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in {401, 403}:
            return f"站点拒绝访问（HTTP {code}），请检查 Cookie、API Key 或 User-Agent"
        if code == 404:
            return "站点页面不存在（HTTP 404），请检查站点地址或站点协议"
        if code == 429:
            return "站点请求过于频繁（HTTP 429），请稍后重试"
        return f"站点返回 HTTP {code}，请稍后重试"
    if isinstance(exc, httpx.TimeoutException):
        return "站点响应超时，请稍后重试"
    if isinstance(exc, httpx.NetworkError):
        return "无法连接站点，请检查地址、代理与网络"
    return safe_error(exc)


def resolve_site_adapter(base_url: str, rss_url: str = "", cookie: str = "", from_moviepilot: bool = False) -> str:
    """Keep adapter details out of the UI; select known special protocols server-side."""
    normalized = base_url.lower()
    if "m-team" in normalized or "mteam" in normalized:
        return "mteam"
    if "torznab" in normalized or "api?t=" in normalized or "t=caps" in normalized:
        return "torznab"
    if from_moviepilot:
        return "nexusphp"
    if cookie.strip() or not rss_url.strip():
        return "nexusphp"
    if rss_url.strip():
        return "rss"
    return "nexusphp"


async def test_site_config(site: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        if site["adapter"] == "mteam":
            result = await MTeamClient().check(site)
        elif site["adapter"] == "nexusphp":
            result = await with_cookie_refresh(site, NexusPHPClient().check)
        elif site["adapter"] == "rss":
            result = await RSSClient().check(site)
        else:
            torrents = await TorznabClient().search(site, "AutoListConnectionProbe")
            result = {"ok": True, "message": f"Torznab 可用，探测返回 {len(torrents)} 条"}
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        if not isinstance(result, dict) or result.get("ok") is not True:
            status = "error"
            message = sanitize_sensitive_text(
                result.get("message") if isinstance(result, dict) else "站点检测返回格式无效"
            ) or "站点连接失败"
        elif result.get("empty"):
            # 登录正常但真实搜索解析不到结果：不算连接失败，但这个站点目前帮不上寻片。
            status = "empty"
            message = sanitize_sensitive_text(result.get("message") or "登录正常，但搜索没有解析到结果")
        else:
            status = "slow" if duration_ms >= SLOW_SITE_THRESHOLD_MS else "ok"
            message = sanitize_sensitive_text(
                result.get("message") or ("连接正常，但响应较慢" if status == "slow" else "连接正常")
            )
    except Exception as exc:
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        status, message = "error", friendly_site_error(exc)
    with connect() as conn:
        conn.execute(
            "UPDATE pt_sites SET last_status=?,last_message=?,last_duration_ms=?,last_tested_at=? WHERE id=?",
            (status, message[:500], duration_ms, utc_now(), site["id"]),
        )
    return {
        "id": site["id"], "name": site["name"], "ok": status in {"ok", "slow"},
        "status": status, "duration_ms": duration_ms, "message": message,
    }


def apply_cookie_groups(groups: dict[str, str], site_id: int | None = None, origin: str = "manual") -> dict[str, Any]:
    """Apply decrypted CookieCloud groups to local sites without changing their User-Agent.

    Cookie 与现有值完全相同的站点记入 ``unchanged``，不写库也不重置账户统计缓存：
    定时拉取每 10 分钟一次，否则会让所有站点反复重新读取账户统计并刷屏日志。
    ``origin``（schedule / expired / manual）只用于记录最近一次整体同步；Cookie 有变化的站点会在后台重新检测。
    """
    updated_ids: list[int] = []
    updated: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []
    with connect() as conn:
        if site_id is None:
            rows = conn.execute("SELECT id,name,base_url,adapter,cookie FROM pt_sites ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT id,name,base_url,adapter,cookie FROM pt_sites WHERE id=?", (site_id,)).fetchall()
        for row in rows:
            host = urlparse(str(row["base_url"])).hostname or ""
            match = cookie_for_host(groups, host)
            if not match:
                missing.append(str(row["name"]))
                continue
            # 与 resolve_site_adapter 保持一致：拿到 Cookie 的 RSS 站点改走 NexusPHP 页面搜索。
            adapter = "nexusphp" if row["adapter"] == "rss" else row["adapter"]
            if match[1] == str(row["cookie"] or "") and adapter == row["adapter"]:
                unchanged.append(str(row["name"]))
                continue
            conn.execute(
                """UPDATE pt_sites
                   SET cookie=?,adapter=?,migration_note=NULL,account_stats_checked_at=NULL,account_stats_error=NULL,
                       cookie_updated_at=?,cookie_source='cookiecloud'
                   WHERE id=?""",
                (match[1], adapter, utc_now(), row["id"]),
            )
            updated.append(str(row["name"]))
            updated_ids.append(int(row["id"]))
    if site_id is None:
        state.last_cookie_sync.clear()
        state.last_cookie_sync.update({
            "at": utc_now(), "origin": origin, "updated": updated, "unchanged": len(unchanged), "missing": missing,
        })
    schedule_site_retests(updated_ids)
    return {"updated": updated, "unchanged": unchanged, "missing": missing}


_retest_tasks: set[asyncio.Task[Any]] = set()


def schedule_site_retests(site_ids: list[int]) -> None:
    """Cookie 换了之后原来的检测结果已经过时：在后台逐个重新检测这些站点。"""
    if not site_ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def run() -> None:
        for site_id in site_ids:
            with connect() as conn:
                row = conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone()
            if row and row["enabled"]:
                await test_site_config(dict(row))

    task = loop.create_task(run())
    _retest_tasks.add(task)
    task.add_done_callback(_retest_tasks.discard)


def _stats_refresh_due(site: dict[str, Any], now: datetime) -> bool:
    checked = str(site.get("account_stats_checked_at") or "")
    if not checked:
        return True
    try:
        checked_at = datetime.fromisoformat(checked.replace("Z", "+00:00"))
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    ttl = SITE_STATS_FAILURE_TTL if site.get("account_stats_error") else SITE_STATS_SUCCESS_TTL
    return now - checked_at >= ttl


async def refresh_site_account_stats(site: dict[str, Any]) -> dict[str, Any]:
    """Refresh one site's cached account counters through its native protocol."""
    checked_at = utc_now()
    try:
        # 没有凭据时不发起网络请求：匿名访问只会得到登录页或 401。
        if site["adapter"] == "mteam":
            if not str(site.get("api_key") or "").strip():
                raise RuntimeError("未配置 M-Team API Key，暂不读取账户统计")
            stats = await MTeamClient().account_stats(site)
        elif site["adapter"] == "nexusphp":
            if not str(site.get("cookie") or "").strip():
                raise RuntimeError("未配置 Cookie，暂不读取账户统计")
            stats = await NexusPHPClient().account_stats(site)
        else:
            raise RuntimeError("RSS/Torznab 协议不提供站点账户统计")
        error = None
    except Exception as exc:
        stats = {}
        error = friendly_site_error(exc)
    with connect() as conn:
        if error:
            conn.execute(
                "UPDATE pt_sites SET account_stats_checked_at=?,account_stats_error=? WHERE id=?",
                (checked_at, error[:500], site["id"]),
            )
        else:
            conn.execute(
                """UPDATE pt_sites SET account_uploaded=?,account_downloaded=?,account_ratio=?,
                          account_bonus=?,account_seeding=?,account_stats_checked_at=?,account_stats_error=NULL
                   WHERE id=?""",
                (
                    stats.get("uploaded"), stats.get("downloaded"), stats.get("ratio"),
                    stats.get("bonus"), stats.get("seeding"), checked_at, site["id"],
                ),
            )
    return {"id": int(site["id"]), "ok": not error, "error": error, **stats}


async def refresh_stale_site_account_stats(limit: int = 2) -> list[dict[str, Any]]:
    """Refresh a small bounded batch; the scheduler calls this once per minute."""
    with connect() as conn:
        sites = [
            dict(row) for row in conn.execute(
                "SELECT * FROM pt_sites WHERE enabled=1 ORDER BY account_stats_checked_at IS NOT NULL,account_stats_checked_at,id"
            ).fetchall()
        ]
    now = datetime.now(timezone.utc)
    due = [site for site in sites if _stats_refresh_due(site, now)][:max(1, min(limit, 4))]
    results: list[dict[str, Any]] = []
    for site in due:
        results.append(await refresh_site_account_stats(site))
    return results


async def fetch_moviepilot_sites() -> list[dict[str, Any]]:
    """Fetch raw configured site records from MoviePilot."""
    from ..config import settings
    from ..outbound import safe_request
    import httpx

    if not settings.mp_base_url or not settings.mp_api_key:
        return []
    headers = {"X-API-KEY": settings.mp_api_key}
    async with httpx.AsyncClient(base_url=settings.mp_base_url, timeout=settings.mp_timeout_seconds, follow_redirects=False) as client:
        response = await safe_request(
            client, "GET", "/api/v1/site/", headers=headers, label="MoviePilot 地址", allow_private=True,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return []


async def sync_sites_from_moviepilot() -> dict[str, Any]:
    """Import and synchronize configured PT sites from MoviePilot."""
    from ..config import APP_VERSION
    from ..logs import event_logger
    from .cookiecloud import cookiecloud_configured, pull_cookiecloud
    from ..util import to_int

    raw_sites = await fetch_moviepilot_sites()
    if not raw_sites:
        return {"ok": False, "total": 0, "sites": [], "cookiecloud_updated": 0, "message": "未从 MoviePilot 获取到任何站点配置"}

    name_counts: dict[str, int] = {}
    for s in raw_sites:
        n = str(s.get("name") or "").strip()
        name_counts[n] = name_counts.get(n, 0) + 1

    synced: list[str] = []
    skipped: list[str] = []
    with connect() as conn:
        existing_rows = conn.execute("SELECT id, name, base_url, cookie, rss_url FROM pt_sites").fetchall()
        by_host = {str(urlparse(str(r["base_url"] or "")).hostname or "").lower(): r for r in existing_rows}
        by_name = {str(r["name"]): r for r in existing_rows}

        for s in raw_sites:
            raw_name = str(s.get("name") or "").strip() or "未命名站点"
            raw_url = str(s.get("url") or "").strip()
            if not raw_url:
                continue
            try:
                raw_url = validate_outbound_url(raw_url, label=f"站点 {raw_name} 地址", allow_private=True).rstrip("/")
            except ValueError:
                skipped.append(f"{raw_name}（地址无效）")
                continue
            host = str(urlparse(raw_url).hostname or "").lower()
            rss_url = str(s.get("rss") or "").strip()
            if rss_url:
                try:
                    rss_url = validate_outbound_url(rss_url, label=f"站点 {raw_name} RSS", allow_private=True)
                except ValueError:
                    rss_url = ""
            cookie = str(s.get("cookie") or "").strip()
            api_key = str(s.get("apikey") or "").strip()
            ua = str(s.get("ua") or "").strip()
            priority = max(1, min(to_int(s.get("pri") or 100), 999))
            # MoviePilot 默认 15 秒，AutoList 按 IMDb 与片名各搜一次，慢站（站点F、站点G）不够用，新站点至少 30 秒。
            timeout = max(30, min(to_int(s.get("timeout") or 30), 60))
            proxy = 1 if s.get("proxy") else 0
            render = 1 if s.get("render") else 0
            is_active = 1 if s.get("is_active", True) else 0

            site_name = raw_name
            if name_counts.get(raw_name, 0) > 1 and host:
                site_name = f"{raw_name} ({host})"

            existing = by_host.get(host) or by_name.get(site_name) or by_name.get(raw_name)
            if existing:
                # 已存在的站点只同步连接信息；名称、优先级、超时、启用与参与搜索、UA 等本地选择保持不变。
                effective_cookie = cookie or str(existing["cookie"] or "")
                effective_rss = rss_url or str(existing["rss_url"] or "")
                adapter = resolve_site_adapter(raw_url, effective_rss, cookie=effective_cookie, from_moviepilot=True)
                conn.execute(
                    """UPDATE pt_sites
                       SET adapter=?, base_url=?, proxy=?, render=?,
                           rss_url=CASE WHEN ? != '' THEN ? ELSE rss_url END,
                           cookie_updated_at=CASE WHEN ? != '' AND ? != COALESCE(cookie, '') THEN ? ELSE cookie_updated_at END,
                           cookie_source=CASE WHEN ? != '' AND ? != COALESCE(cookie, '') THEN 'moviepilot' ELSE cookie_source END,
                           cookie=CASE WHEN ? != '' THEN ? ELSE cookie END,
                           api_key=CASE WHEN ? != '' AND COALESCE(api_key, '') = '' THEN ? ELSE api_key END,
                           user_agent=CASE WHEN COALESCE(user_agent, '') = '' THEN ? ELSE user_agent END
                       WHERE id=?""",
                    (adapter, raw_url, proxy, render, rss_url, rss_url,
                     cookie, cookie, utc_now(), cookie, cookie, cookie, cookie,
                     api_key, api_key, ua, existing["id"]),
                )
                synced.append(str(existing["name"]))
                continue
            adapter = resolve_site_adapter(raw_url, rss_url, cookie=cookie, from_moviepilot=True)
            try:
                site_id = conn.execute(
                    """INSERT INTO pt_sites(name, adapter, base_url, api_key, cookie, user_agent,
                                           priority, timeout_seconds, rss_url, icon_url, proxy,
                                           render, enabled, search_enabled, created_at, cookie_updated_at, cookie_source)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, 1, ?, ?, ?)""",
                    (site_name, adapter, raw_url, api_key, cookie, ua or f"AutoList/{APP_VERSION}", priority, timeout,
                     rss_url, proxy, render, is_active, utc_now(),
                     utc_now() if cookie else None, "moviepilot" if cookie else None),
                ).lastrowid
            except sqlite3.IntegrityError:
                skipped.append(f"{site_name}（名称已存在）")
                continue
            inserted = {"id": site_id, "name": site_name, "base_url": raw_url, "cookie": cookie, "rss_url": rss_url}
            if host:
                by_host[host] = inserted
            by_name[site_name] = inserted
            synced.append(site_name)

    cc_updated = 0
    cc_note = ""
    if cookiecloud_configured():
        try:
            applied = await pull_cookiecloud("manual")
            cc_updated = len(applied["updated"])
        except Exception as exc:
            # CookieCloud 是可选补充：站点同步本身已经完成，只把原因返回给界面。
            detail = getattr(exc, "detail", None)
            cc_note = f"；CookieCloud 未更新：{sanitize_sensitive_text(detail) if isinstance(detail, str) else safe_error(exc)}"

    skipped_note = f"；跳过 {len(skipped)} 个：{'、'.join(skipped[:5])}" if skipped else ""
    message = (
        f"已从 MoviePilot 同步 {len(synced)} 个站点，其中 {cc_updated} 个站点已匹配最新 Cookie"
        f"{skipped_note}{cc_note}。已有站点的名称、优先级、启用与搜索开关保持不变；"
        "新站点按 NexusPHP 页面协议接入，建议同步后执行一次“检测全部站点”"
    )
    event_logger().info(
        "moviepilot_sites_synced",
        extra={"detail": message, "total": len(synced)},
    )
    return {
        "ok": True,
        "total": len(synced),
        "sites": synced,
        "skipped": skipped,
        "cookiecloud_updated": cc_updated,
        "message": message,
    }
