"""PT site adapter selection and connection helpers."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from ..clients import MTeamClient, NexusPHPClient, RSSClient, TorznabClient
from ..cookiecloud import cookie_for_host
from ..database import connect
from ..security import safe_error, sanitize_sensitive_text
from ..util import utc_now

SITE_STATS_SUCCESS_TTL = timedelta(hours=6)
SITE_STATS_FAILURE_TTL = timedelta(hours=1)
SLOW_SITE_THRESHOLD_MS = 3000


def resolve_site_adapter(base_url: str, rss_url: str = "") -> str:
    """Keep adapter details out of the UI; select known special protocols server-side."""
    if rss_url.strip():
        return "rss"
    normalized = base_url.lower()
    if "m-team" in normalized or "mteam" in normalized:
        return "mteam"
    if "torznab" in normalized or "api?t=" in normalized or "t=caps" in normalized:
        return "torznab"
    return "nexusphp"


async def test_site_config(site: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        if site["adapter"] == "mteam":
            result = await MTeamClient().check(site)
        elif site["adapter"] == "nexusphp":
            result = await NexusPHPClient().check(site)
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
        else:
            status = "slow" if duration_ms >= SLOW_SITE_THRESHOLD_MS else "ok"
            message = sanitize_sensitive_text(
                result.get("message") or ("连接正常，但响应较慢" if status == "slow" else "连接正常")
            )
    except Exception as exc:
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        status, message = "error", safe_error(exc)
    with connect() as conn:
        conn.execute(
            "UPDATE pt_sites SET last_status=?,last_message=?,last_duration_ms=?,last_tested_at=? WHERE id=?",
            (status, message[:500], duration_ms, utc_now(), site["id"]),
        )
    return {
        "id": site["id"], "name": site["name"], "ok": status in {"ok", "slow"},
        "status": status, "duration_ms": duration_ms, "message": message,
    }


def apply_cookie_groups(groups: dict[str, str], site_id: int | None = None) -> dict[str, Any]:
    """Apply decrypted CookieCloud groups to local sites without changing their User-Agent."""
    updated: list[str] = []
    missing: list[str] = []
    with connect() as conn:
        if site_id is None:
            rows = conn.execute("SELECT id,name,base_url FROM pt_sites ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT id,name,base_url FROM pt_sites WHERE id=?", (site_id,)).fetchall()
        for row in rows:
            host = urlparse(str(row["base_url"])).hostname or ""
            match = cookie_for_host(groups, host)
            if not match:
                missing.append(str(row["name"]))
                continue
            conn.execute(
                """UPDATE pt_sites
                   SET cookie=?,migration_note=NULL,account_stats_checked_at=NULL,account_stats_error=NULL
                   WHERE id=?""",
                (match[1], row["id"]),
            )
            updated.append(str(row["name"]))
    return {"updated": updated, "missing": missing}


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
        if site["adapter"] == "mteam":
            stats = await MTeamClient().account_stats(site)
        elif site["adapter"] == "nexusphp":
            stats = await NexusPHPClient().account_stats(site)
        else:
            raise RuntimeError("RSS/Torznab 协议不提供站点账户统计")
        error = None
    except Exception as exc:
        stats = {}
        error = safe_error(exc)
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
