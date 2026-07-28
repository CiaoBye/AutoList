"""PT site adapter selection and MoviePilot site snapshot helpers."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

from ..clients import MoviePilotClient, MTeamClient, NexusPHPClient, RSSClient, TorznabClient
from ..config import settings
from ..database import connect
from ..security import safe_error, sanitize_sensitive_text
from ..util import utc_now


def domain_match(domain: str, base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower().removeprefix("www.")
    value = domain.lower().removeprefix("www.")
    return host == value or host.endswith(f".{value}") or value.endswith(f".{host}")


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


async def moviepilot_site_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    client = MoviePilotClient()
    if not settings.mp_base_url or not settings.mp_api_key:
        return [], [], []
    sites, statistics, users = await asyncio.gather(client.sites(), client.site_statistics(), client.site_user_data())
    return (sites if isinstance(sites, list) else [], statistics if isinstance(statistics, list) else [], users if isinstance(users, list) else [])


async def test_site_config(site: dict[str, Any]) -> dict[str, Any]:
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
        status, message = "ok", sanitize_sensitive_text(result.get("message") or "连接正常")
    except Exception as exc:
        status, message = "error", safe_error(exc)
    with connect() as conn:
        conn.execute("UPDATE pt_sites SET last_status=?,last_message=?,last_tested_at=? WHERE id=?", (status, message[:500], utc_now(), site["id"]))
    return {"id": site["id"], "name": site["name"], "ok": status == "ok", "message": message}

