"""动态：提交、寻片、识别、Emby 刷新、同步通知与少量设置变更，按时间倒序。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..database import connect
from ..queries import timeline as queries
from ..queries.films import film_item
from ..security import sanitize_sensitive_text
from .logs import log_events
from ..util import json_ids, to_int
from ..responses import Timeline

router = APIRouter()


TIMELINE_LOG_EVENTS = {
    "playlist_imported": "导入片单",
    "site_added": "新增站点",
    "site_updated": "更新站点",
    "site_deleted": "删除站点",
    "settings_saved": "保存设置",
    "moviepilot_sites_synced": "从 MoviePilot 同步站点",
    "film_reidentified": "修正识别",
}
TASK_STATUS_TEXT = {
    "queued": "排队中",
    "running": "进行中",
    "completed": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "服务重启中断",
    "archived": "已归档",
}
TASK_LEVEL = {"completed": "success", "partial": "warning", "failed": "error", "interrupted": "warning", "cancelled": "info"}


def _film_name(row: Any) -> str:
    return str(row["tmdb_title"] or row["chinese_title"] or row["original_title"] or "未知影片")


def _timeline_db_events(conn: Any, limit: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in queries.recent_submissions(conn, limit):
        name = _film_name(row) if row["original_title"] else str(row["torrent_name"] or "未知影片")
        if row["success"]:
            arrived = row["library_state"] == "in_library"
            title = f"{'已入馆' if arrived else '已提交下载'}：{name}"
            level = "success"
            detail = f"{row['site_name'] or '未知站点'} · {row['torrent_name']}"
        else:
            title, level = f"提交失败：{name}", "error"
            detail = sanitize_sensitive_text(row["message"] or "提交失败", 300)
        events.append({
            "id": f"submit-{row['id']}", "at": row["created_at"], "kind": "submit", "level": level,
            "title": title, "detail": detail, "film_id": row["film_id"],
        })
    for row in queries.recent_search_tasks(conn, limit):
        item_ids = json_ids(row["item_ids_json"]) or []
        film_id = item_ids[0] if len(item_ids) == 1 else None
        if film_id is not None:
            film = film_item(conn, film_id)
            title = f"寻片：{_film_name(film) if film else '已删除的影片'}"
        else:
            title = f"批量寻片 {row['total']} 部 · {row['playlist_name']}"
        status = str(row["status"])
        if status in {"queued", "running"}:
            detail = f"{TASK_STATUS_TEXT[status]} · {row['completed']} / {row['total']}"
        elif status in {"completed", "partial"}:
            detail = f"{TASK_STATUS_TEXT[status]} · {row['matched']} 部找到合格资源"
        else:
            detail = TASK_STATUS_TEXT.get(status, status)
        if row["error_message"] and status in {"failed", "partial"}:
            detail += f" · {sanitize_sensitive_text(row['error_message'], 200)}"
        events.append({
            "id": f"search-{row['id']}", "at": row["updated_at"] or row["created_at"], "kind": "search",
            "level": TASK_LEVEL.get(status, "info"), "title": title, "detail": detail, "film_id": film_id,
            "film_ids": item_ids, "task_id": row["id"], "task_status": status,
        })
    for kind, label in (("recognition", "TMDB 识别"), ("library", "刷新 Emby 状态")):
        for row in queries.recent_playlist_tasks(conn, kind, limit):
            status = str(row["status"])
            if status in {"queued", "running"}:
                progress = f"{row['completed']} / {row['total']}"
            elif kind == "recognition" and row["mode"] == "verify":
                progress = f"核对 {row['total']} 部，改正 {row['corrected']} 部"
            elif kind == "recognition":
                progress = f"{row['amount']} / {row['total']} 部已识别"
            else:
                progress = f"{row['amount']} 部在 Emby 中"
            detail = f"{TASK_STATUS_TEXT.get(status, status)} · {progress}"
            if row["error_message"] and status in {"failed", "partial"}:
                detail += f" · {sanitize_sensitive_text(row['error_message'], 160)}"
            events.append({
                "id": f"{kind}-{row['id']}", "at": row["updated_at"] or row["created_at"], "kind": kind,
                "level": TASK_LEVEL.get(status, "info"),
                "title": f"{'按 IMDb 校准' if row['mode'] == 'verify' else label} · {row['playlist_name']}",
                "detail": detail, "film_id": None,
            })
    for row in queries.recent_notifications(conn, limit):
        level = {"success": "success", "error": "error", "warning": "warning"}.get(str(row["level"]), "info")
        events.append({
            "id": f"notice-{row['id']}", "at": row["created_at"], "kind": "notice", "level": level,
            "title": str(row["title"]), "detail": sanitize_sensitive_text(row["message"] or "", 300), "film_id": None,
        })
    return events


async def _timeline_log_events(limit: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, event in enumerate(await log_events(limit=min(500, limit * 3))):
        name = str(event.get("event") or "")
        if name not in TIMELINE_LOG_EVENTS:
            continue
        detail = str(event.get("detail") or event.get("movie") or event.get("site") or "")
        level = {"ERROR": "error", "WARNING": "warning"}.get(str(event.get("level") or "").upper(), "info")
        events.append({
            "id": f"log-{event.get('ts')}-{index}", "at": event.get("ts"), "kind": "system", "level": level,
            "title": TIMELINE_LOG_EVENTS[name], "detail": sanitize_sensitive_text(detail, 300), "film_id": None,
        })
    return events


@router.get("/api/timeline", response_model=Timeline)
async def timeline(type: str = "all", film_id: int | None = None, limit: int = 120) -> dict[str, Any]:
    """动态：提交、寻片、识别、Emby 刷新、同步通知与少量设置变更，按时间倒序。"""
    if type not in {"all", "films", "system"}:
        raise HTTPException(422, "不支持的动态类型")
    safe_limit = max(1, min(to_int(limit, 120), 300))
    with connect() as conn:
        events = _timeline_db_events(conn, safe_limit)
    events.extend(await _timeline_log_events(safe_limit))
    if film_id is not None:
        events = [event for event in events if event.get("film_id") == film_id or film_id in (event.get("film_ids") or [])]
    elif type == "films":
        events = [event for event in events if event.get("film_id") is not None]
    elif type == "system":
        events = [event for event in events if event.get("film_id") is None]
    events.sort(key=lambda event: str(event.get("at") or ""), reverse=True)
    for event in events:
        event.pop("film_ids", None)
    return {"items": events[:safe_limit]}
