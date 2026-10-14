"""站点连续失败自动暂停：避免反复请求一个明显不可用的站点；检测恢复正常后自动重新启用。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..logs import event_logger
from ..queries import sites as queries

AUTO_PAUSE_FAILURES = 5  # 连续这么多次搜索失败，并且最近一小时内没有成功过，就自动暂停
RECENT_SUCCESS_GRACE = timedelta(hours=1)


def _parse(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def maybe_auto_pause(conn: Any, site_id: int, site_name: str) -> bool:
    """搜索失败后调用：连续失败次数够多、且最近一小时内没有成功过时暂停该站点。"""
    attempts = queries.recent_search_attempts(conn, site_id, AUTO_PAUSE_FAILURES + 1)
    streak = 0
    for attempt in attempts:
        if attempt["status"] == "success":
            break
        streak += 1
    if streak < AUTO_PAUSE_FAILURES:
        return False
    if streak < len(attempts):  # 失败之前有过成功：成功不能是刚刚发生的
        success_at = _parse(attempts[streak]["finished_at"])
        if success_at and datetime.now(timezone.utc) - success_at < RECENT_SUCCESS_GRACE:
            return False
    reason = f"连续 {streak} 次搜索失败"
    if not queries.pause_site(conn, site_id, reason):
        return False
    event_logger().warning("site_auto_paused", extra={"site": site_name, "detail": f"站点【{site_name}】{reason}，已暂停搜索，检测恢复后自动启用"})
    return True


def resume_after_check(conn: Any, site_id: int, site_name: str) -> bool:
    """检测恢复正常后调用：自动暂停的站点重新参与搜索。"""
    if not queries.resume_site(conn, site_id):
        return False
    event_logger().info("site_auto_resumed", extra={"site": site_name, "detail": f"站点【{site_name}】检测正常，已恢复搜索"})
    return True
