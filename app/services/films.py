"""Film-centred projection of playlists, searches, the pick list and downloads.

新界面以“每一部电影”为中心：每部影片只有一个当前状态，由这里根据
识别结果、Emby、搜索任务、候选、待入馆清单、下载历史与 Transmission
统一计算。所有页面共用同一套状态，避免同一部电影在不同页面状态不一致。

计算是批量的（每类数据一次查询 + 一次 Transmission 快照），不逐片查询。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import settings
from ..database import connect
from ..domain.titles import canonical_item_original_title, canonical_item_title, canonical_item_year
from ..security import signed_media_url
from ..state import raw_candidates
from ..util import to_int, utc_now
from .history import _active_history_matches
from .search import _current_downloads_cached

# 主状态，按入馆流程顺序排列。
FILM_STATUSES: tuple[str, ...] = (
    "unrecognized", "unchecked", "missing", "searching", "candidates", "selected", "downloading", "in_library",
)
FILM_STATUS_LABELS = {
    "unrecognized": "待识别",
    "unchecked": "待核对",
    "missing": "缺片",
    "searching": "寻片中",
    "candidates": "有候选",
    "selected": "已选定",
    "downloading": "下载中",
    "in_library": "已入馆",
}
# 异常以标记挂在影片上，不改变主状态。
FILM_ISSUE_LABELS = {
    "no_eligible": "无合格资源",
    "submit_failed": "提交失败",
    "context_expired": "候选已过期",
}


@dataclass
class _Signals:
    """Batched per-item signals gathered before status resolution."""

    active_search: set[int] = field(default_factory=set)
    searched: set[int] = field(default_factory=set)
    eligible: dict[int, list[str]] = field(default_factory=dict)
    selected: dict[int, list[str]] = field(default_factory=dict)
    submitted: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    last_submit_failed: set[int] = field(default_factory=set)
    downloading_active: set[int] = field(default_factory=set)
    transmission_state: str = "unknown"


def _placeholders(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


def _snapshot_item_ids(raw: Any) -> list[int] | None:
    if raw is None:
        return None
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(values, list):
        return None
    return [to_int(value) for value in values if str(value).isdigit()]


def _active_search_items(conn: Any, item_ids: set[int]) -> set[int]:
    active: set[int] = set()
    for task in conn.execute(
        "SELECT playlist_id,range_start,range_end,item_ids_json FROM search_tasks WHERE status IN ('queued','running')"
    ).fetchall():
        snapshot = _snapshot_item_ids(task["item_ids_json"])
        if snapshot is not None:
            active.update(item_id for item_id in snapshot if item_id in item_ids)
            continue
        for row in conn.execute(
            "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no BETWEEN ? AND ?",
            (task["playlist_id"], task["range_start"], task["range_end"]),
        ).fetchall():
            if to_int(row["id"]) in item_ids:
                active.add(to_int(row["id"]))
    return active


def current_candidate_tasks(conn: Any, item_ids: Iterable[int]) -> dict[int, list[int]]:
    """每部影片当前有效的候选来源任务。

    只看最近一次产生候选的搜索任务，旧任务的候选不再代表当前可选资源；但“重试失败的站点”
    只重搜失败的影片与站点组合，它的候选要与父任务链合并，否则重试后其他站点的候选会消失。
    """
    ids = sorted({to_int(item_id) for item_id in item_ids})
    if not ids:
        return {}
    latest = conn.execute(
        f"""SELECT playlist_item_id, MAX(task_id) AS task_id FROM candidates
            WHERE playlist_item_id IN ({_placeholders(ids)}) GROUP BY playlist_item_id""",  # nosec B608
        ids,
    ).fetchall()
    tasks: dict[int, Any] = {}
    chains: dict[int, list[int]] = {}
    for row in latest:
        chain: list[int] = []
        task_id: int | None = to_int(row["task_id"])
        while task_id and task_id not in chain:
            chain.append(task_id)
            if task_id not in tasks:
                tasks[task_id] = conn.execute(
                    "SELECT trigger,parent_task_id FROM search_tasks WHERE id=?", (task_id,),
                ).fetchone()
            task = tasks[task_id]
            task_id = to_int(task["parent_task_id"]) if task and task["trigger"] == "retry" and task["parent_task_id"] else None
        chains[to_int(row["playlist_item_id"])] = chain
    return chains


def _collect_signals(conn: Any, items: list[dict[str, Any]]) -> _Signals:
    signals = _Signals()
    item_ids = {to_int(item["id"]) for item in items}
    if not item_ids:
        return signals
    ids = sorted(item_ids)
    marks = _placeholders(ids)
    signals.active_search = _active_search_items(conn, item_ids)
    signals.searched = {
        to_int(row["playlist_item_id"]) for row in conn.execute(
            f"SELECT DISTINCT playlist_item_id FROM search_attempts WHERE playlist_item_id IN ({marks})",  # nosec B608
            ids,
        ).fetchall()
    }
    for item_id, task_ids in current_candidate_tasks(conn, ids).items():
        task_marks = _placeholders(task_ids)
        signals.eligible[item_id] = [
            str(candidate["id"]) for candidate in conn.execute(
                f"""SELECT id FROM candidates
                    WHERE playlist_item_id=? AND task_id IN ({task_marks}) AND eligibility='eligible'""",  # nosec B608
                (item_id, *task_ids),
            ).fetchall()
        ]
    for row in conn.execute(
        f"""SELECT c.playlist_item_id, c.id FROM cart_items cart JOIN candidates c ON c.id=cart.candidate_id
            WHERE c.playlist_item_id IN ({marks})""",  # nosec B608
        ids,
    ).fetchall():
        signals.selected.setdefault(to_int(row["playlist_item_id"]), []).append(str(row["id"]))
    history_rows = conn.execute(
        f"""SELECT h.id, h.success, h.submission_hash, h.torrent_name,
                   COALESCE(h.playlist_item_id, c.playlist_item_id) AS item_id
            FROM download_history h LEFT JOIN candidates c ON c.id=h.candidate_id
            WHERE COALESCE(h.playlist_item_id, c.playlist_item_id) IN ({marks})
            ORDER BY h.id""",  # nosec B608
        ids,
    ).fetchall()
    latest_by_item: dict[int, bool] = {}
    for row in history_rows:
        item_id = to_int(row["item_id"])
        latest_by_item[item_id] = bool(row["success"])
        if row["success"]:
            signals.submitted.setdefault(item_id, []).append(dict(row))
    signals.last_submit_failed = {item_id for item_id, success in latest_by_item.items() if not success}
    return signals


async def _attach_transmission(signals: _Signals, items_by_id: dict[int, dict[str, Any]]) -> None:
    if not signals.submitted:
        return
    torrents, state = await _current_downloads_cached()
    signals.transmission_state = state
    if state == "unknown":
        return
    histories: list[dict[str, Any]] = []
    for item_id, rows in signals.submitted.items():
        for row in rows:
            histories.append({**row, "_playlist_item": items_by_id.get(item_id)})
    matched, _ambiguous = _active_history_matches(histories, torrents)
    for row in histories:
        if to_int(row["id"]) in matched:
            signals.downloading_active.add(to_int(row["item_id"]))


def _resolve(item: dict[str, Any], signals: _Signals) -> tuple[str, list[str], str | None]:
    """Return (status, issues, transfer) following the documented priority order."""
    item_id = to_int(item["id"])
    issues: list[str] = []
    if item_id in signals.last_submit_failed:
        issues.append("submit_failed")
    library_state = str(item.get("library_state") or "unknown")
    if library_state == "in_library":
        return "in_library", issues, None
    if item_id in signals.submitted:
        if item_id in signals.downloading_active:
            transfer = "active"
        elif signals.transmission_state == "unknown":
            transfer = "unknown"
        else:
            # 已提交但 Transmission 中不在下载：可能已完成、等待 MoviePilot 整理或 Emby 入库。
            transfer = "waiting_library"
        return "downloading", issues, transfer
    selected = signals.selected.get(item_id)
    if selected:
        if not any(candidate_id in raw_candidates for candidate_id in selected):
            issues.append("context_expired")
        return "selected", issues, None
    if item_id in signals.active_search:
        return "searching", issues, None
    eligible = signals.eligible.get(item_id) or []
    if eligible:
        if not any(candidate_id in raw_candidates for candidate_id in eligible):
            issues.append("context_expired")
        return "candidates", issues, None
    if item_id in signals.searched:
        issues.append("no_eligible")
    if item.get("tmdb_id") is None:
        return "unrecognized", issues, None
    if library_state == "unknown":
        # Emby 未配置、从未检查或查询失败：不能把“无法确认”当作缺片。
        return "unchecked", issues, None
    return "missing", issues, None


# 尚未查询 fanart.tv 时的版本号。1.49 及之前用过 “0” 并被浏览器长期缓存，换一个值避开旧缓存。
FANART_PENDING_VERSION = "pending"


def fanart_version(poster: str | None) -> str:
    """海报地址的版本参数：随所选 fanart 海报变化，未查询时为固定的待定值。"""
    return hashlib.sha1(poster.encode("utf-8")).hexdigest()[:10] if poster else FANART_PENDING_VERSION


def backdrop_url(item: dict[str, Any]) -> str | None:
    """影片详情横幅：fanart.tv 剧照优先，其次 TMDB 剧照；确认都没有时不生成地址。"""
    if not item.get("tmdb_id") or not (settings.fanart_api_key or settings.tmdb_api_key):
        return None
    fanart, tmdb = item.get("fanart_backdrop_url"), item.get("tmdb_backdrop_path")
    if fanart == "" and tmdb == "":
        return None
    return signed_media_url(f"/api/playlist-items/{item['id']}/backdrop?v={fanart_version(fanart or tmdb or None)}")


def poster_url(item: dict[str, Any]) -> str | None:
    """海报来源优先级：fanart.tv → Emby → TMDB。

    fanart.tv 尚未查过（NULL）时也走 fanart 地址，由服务端现查并在没有海报时就地回退；
    ``v`` 随 fanart 海报地址变化，重新识别后浏览器不会继续显示旧海报。
    """
    fanart = item.get("fanart_poster_url")
    if settings.fanart_api_key and item.get("tmdb_id") and fanart != "":
        return signed_media_url(f"/api/playlist-items/{item['id']}/fanart-poster?v={fanart_version(fanart)}")
    if item.get("emby_item_id") and item.get("emby_image_tag"):
        return signed_media_url(f"/api/playlist-items/{item['id']}/poster?tag={item['emby_image_tag']}")
    # 未配置 TMDB 时不生成海报地址，避免每张海报都请求一次注定失败的代理。
    if item.get("tmdb_id") and item.get("tmdb_poster_path") != "" and settings.tmdb_api_key:
        return signed_media_url(f"/api/playlist-items/{item['id']}/tmdb-poster")
    return None


def film_summary(item: dict[str, Any], status: str, issues: list[str], transfer: str | None) -> dict[str, Any]:
    return {
        "id": to_int(item["id"]),
        "playlist_id": to_int(item["playlist_id"]),
        "rank_no": item.get("rank_no"),
        "title": canonical_item_title(item),
        "original_title": canonical_item_original_title(item),
        "year": canonical_item_year(item),
        "tmdb_id": item.get("tmdb_id"),
        "imdb_id": item.get("tmdb_imdb_id") or item.get("imdb_id"),
        "library_state": item.get("library_state"),
        "status": status,
        "status_label": FILM_STATUS_LABELS[status],
        "issues": issues,
        "issue_labels": [FILM_ISSUE_LABELS[issue] for issue in issues],
        "transfer": transfer,
        "poster_url": poster_url(item),
    }


async def project_films(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute the unified status for a batch of playlist items."""
    with connect() as conn:
        signals = _collect_signals(conn, items)
    await _attach_transmission(signals, {to_int(item["id"]): item for item in items})
    return [film_summary(item, *_resolve(item, signals)) for item in items]


def status_counts(films: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in FILM_STATUSES}
    for film in films:
        counts[film["status"]] += 1
    counts["all"] = len(films)
    for issue in FILM_ISSUE_LABELS:
        counts[f"issue:{issue}"] = sum(1 for film in films if issue in film["issues"])
    return counts


def matches_query(film: dict[str, Any], query: str) -> bool:
    needle = query.strip().casefold()
    if not needle:
        return True
    haystack = " ".join(
        str(value or "") for value in (film["title"], film["original_title"], film["imdb_id"], film["year"], film["rank_no"])
    ).casefold()
    return needle in haystack


def reidentify_item(item_id: int, media: dict[str, Any], fallback_imdb: str | None = None) -> None:
    """Replace a film's TMDB identity (re-recognition or manual choice).

    与首次识别不同，这里整体替换身份：海报路径按新结果重写（没有则清空待补取），
    Emby 匹配结果作废并交给调用方重新核对，避免沿用错误影片的海报与入库状态。
    """
    from .recognition import tmdb_item_values, tmdb_original_language, tmdb_poster_path

    values = tmdb_item_values(media, fallback_imdb)
    with connect() as conn:
        conn.execute(
            """UPDATE playlist_items
               SET tmdb_id=?,tmdb_title=?,tmdb_original_title=?,tmdb_year=?,tmdb_imdb_id=?,tmdb_checked_at=?,
                   tmdb_poster_path=?,tmdb_original_language=?,fanart_poster_url=NULL,
                   fanart_backdrop_url=NULL,tmdb_backdrop_path=NULL,
                   emby_item_id=NULL,emby_image_tag=NULL,library_state='unknown',library_checked_at=NULL
               WHERE id=?""",
            (*values, tmdb_poster_path(media), tmdb_original_language(media), item_id),
        )


def store_library_state(item_id: int, state: str, emby_item_id: str | None, image_tag: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE playlist_items SET library_state=?,library_checked_at=?,emby_item_id=?,emby_image_tag=? WHERE id=?",
            (state, utc_now(), emby_item_id, image_tag, item_id),
        )
