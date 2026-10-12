"""站况：Cookie 健康、近期搜索与提交统计、Transmission 里各站点的做种情况。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..queries import site_status as queries
from ..queries.sites import sites_with_recent_search_stats
from ..sites import profile_for
from ..util import to_int
from .downloads import SKIP_LABELS
from .transmission_snapshot import _current_downloads_cached

COOKIE_EXPIRED_TEXT = "Cookie 已失效"
RECENT_ATTEMPTS = 30  # 数连续失败时最多回看的搜索次数
STREAK_ALERT = 3  # 连续这么多次搜索失败就提醒
HISTORY_DAYS = 14
STATS_DAYS = 30


def _parse(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _label_site(labels: Any) -> str | None:
    for label in labels or []:
        text = str(label).strip()
        if text and text.casefold() not in SKIP_LABELS:
            return text
    return None


def _failure_streak(attempts: list[dict[str, Any]]) -> tuple[int, bool]:
    """从最近一次起连续失败的次数，以及这些失败里是否有 Cookie 失效。"""
    streak, expired = 0, False
    for attempt in attempts:
        if attempt["status"] == "success":
            break
        streak += 1
        expired = expired or COOKIE_EXPIRED_TEXT in str(attempt.get("error_message") or "")
    return streak, expired


def cookie_health(site: dict[str, Any], streak: int, expired: bool) -> dict[str, Any]:
    """Cookie 状态：expired 失效、missing 没填、failing 连续失败、ok 正常、none 不使用 Cookie 的站点。"""
    uses_cookie = bool(site.get("cookie")) or (site.get("adapter") == "nexusphp" and not site.get("api_key"))
    if not uses_cookie:
        return {"state": "none", "text": "不使用 Cookie"}
    if not site.get("cookie"):
        return {"state": "missing", "text": "还没有 Cookie"}
    if expired or (COOKIE_EXPIRED_TEXT in str(site.get("last_message") or "") and site.get("last_status") == "error"):
        return {"state": "expired", "text": "Cookie 已失效，站点返回登录页"}
    if streak >= STREAK_ALERT:
        return {"state": "failing", "text": f"连续 {streak}{'+' if streak >= RECENT_ATTEMPTS else ''} 次搜索失败"}
    return {"state": "ok", "text": "正常"}


async def build_site_status(conn: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    sites = sites_with_recent_search_stats(conn)
    days = queries.attempt_days(conn, HISTORY_DAYS)
    submissions = queries.submission_counts(conn, STATS_DAYS)
    torrents, transmission_state = await _current_downloads_cached()
    # 种子归到哪个站点：优先按提交记录里的 hash，其次按 MoviePilot 写入的站点名标签。
    submitted = queries.submitted_sites_by_hash(conn)
    by_label: dict[str, dict[str, int]] = {}
    for torrent in torrents:
        site_name = submitted.get(str(torrent.get("hashString") or "").lower()) or _label_site(torrent.get("labels"))
        if site_name:
            entry = by_label.setdefault(site_name.lower(), {"count": 0, "size": 0})
            entry["count"] += 1
            entry["size"] += to_int(torrent.get("totalSize") or 0)
    rows = []
    for site in sites:
        site_id = to_int(site["id"])
        attempts = queries.recent_attempts(conn, site_id, RECENT_ATTEMPTS)
        streak, expired = _failure_streak(attempts)
        health = cookie_health(site, streak, expired)
        updated = _parse(site.get("cookie_updated_at"))
        last_success = next((attempt["finished_at"] for attempt in attempts if attempt["status"] == "success"), None)
        series = []
        for offset in range(HISTORY_DAYS - 1, -1, -1):
            day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
            entry = days.get(site_id, {}).get(day, {"total": 0, "failed": 0})
            series.append({"day": day, **entry})
        total = to_int(site.get("search_total") or 0)
        succeeded = to_int(site.get("search_succeeded") or 0)
        sent = submissions.get(str(site["name"]), {"total": 0, "succeeded": 0})
        seeding = by_label.get(str(site["name"]).lower()) if transmission_state != "unknown" else None
        active = bool(site["enabled"])
        problem = active and health["state"] in {"expired", "missing", "failing"}
        rows.append({
            "id": site_id,
            "name": site["name"],
            "enabled": active,
            "search_enabled": bool(site["search_enabled"]),
            "profile": profile_for(str(site.get("base_url") or ""), has_api_key=bool(str(site.get("api_key") or "").strip())).key,
            "cookie": {
                **health,
                "source": site.get("cookie_source"),
                "updated_at": site.get("cookie_updated_at"),
                "age_days": (now - updated).days if updated else None,
            },
            "failure_streak": streak,
            "last_success_at": last_success,
            "last_status": site.get("last_status"),
            "last_message": site.get("last_message"),
            "search": {
                "total": total,
                "succeeded": succeeded,
                "success_rate": round(succeeded / total * 100, 1) if total else None,
                "average_ms": to_int(site.get("search_average_ms") or 0) if total else None,
                "result_count": to_int(site.get("search_result_count") or 0),
            },
            "submissions": {
                **sent,
                "success_rate": round(sent["succeeded"] / sent["total"] * 100, 1) if sent["total"] else None,
            },
            "seeding": seeding,
            "account": {
                "uploaded": site.get("account_uploaded"),
                "downloaded": site.get("account_downloaded"),
                "ratio": site.get("account_ratio"),
            },
            "history": series,
            "problem": problem,
        })
    return {
        "sites": rows,
        "problem_count": sum(1 for row in rows if row["problem"]),
        "transmission_available": transmission_state != "unknown",
        "history_days": HISTORY_DAYS,
        "stats_days": STATS_DAYS,
    }
