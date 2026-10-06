"""Film-centred projection of playlists, searches, the pick list and downloads.

新界面以“每一部电影”为中心：每部影片只有一个当前状态，由这里根据
识别结果、Emby、搜索任务、候选、待入馆清单、下载历史与 Transmission
统一计算。所有页面共用同一套状态，避免同一部电影在不同页面状态不一致。

计算是批量的（每类数据一次查询 + 一次 Transmission 快照），不逐片查询。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import settings
from ..database import connect
from ..domain.titles import (
    canonical_item_original_title, canonical_item_title, canonical_item_year, normalized_download_name,
    strict_torrent_matches_item,
)
from ..queries import downloads as download_queries
from ..security import signed_media_url
from ..state import forget_raw_candidate, raw_candidates
from ..util import looks_like_series, to_int, utc_now
from .history import _active_history_matches, _unfinished, long_stalled, organize_failures, transfer_info
from .search import _current_downloads_cached

HASH_PATTERN = re.compile(r"[0-9a-f]{40}")

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
    "organize_failed": "整理失败",
    "download_stalled": "下载停滞",
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
    # 正在下载的影片在 Transmission 里的状况（stalled / paused / error 等），用于影片状态的补充说明。
    transfer_states: dict[int, str] = field(default_factory=dict)
    # 停滞超过 STALLED_ALERT_HOURS 的下载，与 MoviePilot 整理失败的影片。
    stalled_long: set[int] = field(default_factory=set)
    # 有没停滞的下载在进行的影片：换了资源之后，旧的停滞种子还留在 Transmission 里也不再算“下载停滞”。
    healthy: set[int] = field(default_factory=set)
    # 每部影片未下完的种子，以及没停滞的那些里最近一次加入的时间（用于找出被新资源取代的停滞旧种子）。
    active: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    newest_healthy: dict[int, int] = field(default_factory=dict)
    organize_failed: set[int] = field(default_factory=set)
    # 不是经 AutoList 提交、但 Transmission 里确有对应种子的影片（在 MoviePilot 或 Transmission 里手动添加）。
    external: set[int] = field(default_factory=set)
    transmission_state: str = "unknown"
    # 仍有可用下载上下文的候选（加密存库，7 天有效）。
    contexts: set[str] = field(default_factory=set)


def _note_active(signals: _Signals, item_id: int, torrent: dict[str, Any]) -> None:
    """记下影片的一个未下完的种子；同一部影片有多个时，只要有一个没停滞就不算停滞。"""
    signals.downloading_active.add(item_id)
    signals.active.setdefault(item_id, []).append(torrent)
    if long_stalled(torrent):
        if item_id not in signals.healthy:
            signals.stalled_long.add(item_id)
            signals.transfer_states[item_id] = transfer_info(torrent)["state"]
        return
    signals.healthy.add(item_id)
    signals.newest_healthy[item_id] = max(signals.newest_healthy.get(item_id, 0), to_int(torrent.get("addedDate")))
    signals.stalled_long.discard(item_id)
    signals.transfer_states[item_id] = transfer_info(torrent)["state"]


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
        "SELECT playlist_id,range_start,range_end,item_ids_json,done_item_ids_json FROM search_tasks WHERE status IN ('queued','running')"
    ).fetchall():
        # 任务进行中已搜完的影片按搜索结果显示（待挑选 / 无合格资源），不再等整批结束。
        done = set(_snapshot_item_ids(task["done_item_ids_json"]) or [])
        snapshot = _snapshot_item_ids(task["item_ids_json"])
        if snapshot is not None:
            active.update(item_id for item_id in snapshot if item_id in item_ids and item_id not in done)
            continue
        for row in conn.execute(
            "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no BETWEEN ? AND ?",
            (task["playlist_id"], task["range_start"], task["range_end"]),
        ).fetchall():
            if to_int(row["id"]) in item_ids and to_int(row["id"]) not in done:
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
        f"""SELECT c.playlist_item_id, c.id FROM selection_items sel JOIN candidates c ON c.id=sel.candidate_id
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
    signals.contexts = raw_candidates.available(
        sorted({candidate for ids in (*signals.eligible.values(), *signals.selected.values()) for candidate in ids})
    )
    return signals


# 手动添加的种子下完并整理后会一直在 Transmission 里做种；超过这个天数仍不见入馆的，不再当作“等待入馆”。
EXTERNAL_FINISHED_DAYS = 7


def _torrent_hash(torrent: dict[str, Any]) -> str:
    return str(torrent.get("hashString") or "").strip().casefold()


async def _attach_transmission(
    signals: _Signals, items_by_id: dict[int, dict[str, Any]], live: list[dict[str, Any]] | None = None,
) -> None:
    """``live`` 给出时（本轮刚实时读到的列表）直接用它，不再读缓存；缓存读取失败时会沿用旧结果，只适合展示。"""
    torrents, state = (live, "known_present" if live else "known_empty") if live is not None else await _current_downloads_cached()
    signals.transmission_state = state
    if state == "unknown":
        return
    failures = await organize_failures() if signals.submitted else {}
    histories: list[dict[str, Any]] = []
    for item_id, rows in signals.submitted.items():
        for row in rows:
            histories.append({**row, "_playlist_item": items_by_id.get(item_id)})
    matched, _ambiguous = _active_history_matches(histories, torrents)
    for row in histories:
        if to_int(row["id"]) in matched:
            _note_active(signals, to_int(row["item_id"]), matched[to_int(row["id"])])
    for row in histories:
        if str(row.get("submission_hash") or "").strip().casefold() in failures and to_int(row["id"]) not in matched:
            signals.organize_failed.add(to_int(row["item_id"]))
    known_hashes = {str(row.get("submission_hash") or "").strip().casefold() for row in histories}
    await _attach_external(signals, items_by_id, torrents, known_hashes, failures)


# 种子名与影片的严格匹配很费 CPU（每对要做标题切词），而种子和影片都很少变：按（种子名、影片、影片的标题字段）记住结果，
# 没记过的一批放到线程里算，避免卡住事件循环（藏馆、挑选等页面的请求同时在等）。
_NAME_MATCH_FIELDS = ("original_title", "chinese_title", "tmdb_title", "tmdb_original_title", "year", "tmdb_year", "tmdb_alt_titles_json")
_NAME_MATCH_LIMIT = 200_000
_name_matches: dict[tuple[str, int, int], bool] = {}


def _item_signature(item: dict[str, Any]) -> int:
    return hash(tuple(str(item.get(field) or "") for field in _NAME_MATCH_FIELDS))


def _match_key(name: str, item: dict[str, Any]) -> tuple[str, int, int]:
    return (name, to_int(item["id"]), _item_signature(item))


def _external_matches(
    torrent: dict[str, Any], entry: dict[str, Any] | None, items: list[dict[str, Any]], by_tmdb: dict[int, list[int]],
) -> list[int]:
    """一个种子对应片单里的哪些影片：先看识别出的 TMDB 编号，识别不出时按片名与年份，且只认唯一的一部。"""
    name = str(torrent.get("name") or "")
    if entry and entry.get("media_type") == "tv" or looks_like_series(name):
        return []  # 剧集不是片单里的影片；它的 TMDB 编号与电影的编号重号，不能拿来对影片
    if entry and entry.get("tmdb_id"):
        # 只有确认是电影的编号才可信；类型未知（还没识别出）时先不对，等识别出类型后再对。
        return by_tmdb.get(to_int(entry["tmdb_id"]), []) if entry.get("media_type") == "movie" else []
    found = [item for item in items if _name_matches.get(_match_key(name, item))]
    if len({to_int(item.get("tmdb_id")) or to_int(item["id"]) for item in found}) != 1:
        return []
    return [to_int(item["id"]) for item in found]


async def _attach_external(
    signals: _Signals, items_by_id: dict[int, dict[str, Any]], torrents: list[dict[str, Any]],
    known_hashes: set[str], failures: dict[str, str],
) -> None:
    """Transmission 里不是经 AutoList 提交、却对得上片单影片的种子，也算这部影片的下载。"""
    items = [item for item in items_by_id.values() if item.get("library_state") != "in_library"]
    unknown = [torrent for torrent in torrents if _torrent_hash(torrent) and _torrent_hash(torrent) not in known_hashes]
    if not items or not unknown:
        return
    by_tmdb: dict[int, list[int]] = {}
    for item in items:
        if item.get("tmdb_id"):
            by_tmdb.setdefault(to_int(item["tmdb_id"]), []).append(to_int(item["id"]))
    with connect() as conn:
        media = download_queries.torrent_media(conn, [_torrent_hash(torrent) for torrent in unknown])
    cutoff = time.time() - EXTERNAL_FINISHED_DAYS * 86400
    best: dict[int, dict[str, Any]] = {}
    running: dict[int, list[dict[str, Any]]] = {}  # 每部影片全部未下完的外部种子
    pending = {
        _match_key(name, item): (name, item)
        for torrent in unknown
        if not (entry := media.get(_torrent_hash(torrent))) or not entry.get("tmdb_id")
        if not looks_like_series(torrent.get("name"))
        if _unfinished(torrent) or to_int(torrent.get("addedDate")) >= cutoff
        for name in [str(torrent.get("name") or "")]
        for item in items
        if _match_key(name, item) not in _name_matches
    }
    if pending:
        keys = list(pending)
        results = await asyncio.to_thread(lambda: [strict_torrent_matches_item(pending[key][1], pending[key][0]) for key in keys])
        if len(_name_matches) + len(keys) > _NAME_MATCH_LIMIT:
            _name_matches.clear()
        _name_matches.update(zip(keys, results))
    for torrent in unknown:
        if not _unfinished(torrent) and to_int(torrent.get("addedDate")) < cutoff:
            continue
        for item_id in _external_matches(torrent, media.get(_torrent_hash(torrent)), items, by_tmdb):
            if _unfinished(torrent):
                running.setdefault(item_id, []).append(torrent)
            current = best.get(item_id)
            # 同一部影片有多个种子时，优先未下完的，其次最近加入的。
            rank = (_unfinished(torrent), to_int(torrent.get("addedDate")))
            if current is None or rank > (_unfinished(current), to_int(current.get("addedDate"))):
                best[item_id] = torrent
    if not failures and any(not _unfinished(torrent) for torrent in best.values()):
        failures = await organize_failures()
    for item_id, torrent in best.items():
        signals.external.add(item_id)
        if item_id in running:
            # 同一部影片可能有多个下载：全部记下，才能判断“有没停滞的在进行”，也才能找出被取代的停滞旧任务。
            for active in running[item_id]:
                _note_active(signals, item_id, active)
        elif _torrent_hash(torrent) in failures:
            signals.organize_failed.add(item_id)


def _resolve(item: dict[str, Any], signals: _Signals) -> tuple[str, list[str], str | None]:
    """Return (status, issues, transfer) following the documented priority order."""
    item_id = to_int(item["id"])
    issues: list[str] = []
    if item_id in signals.last_submit_failed:
        issues.append("submit_failed")
    library_state = str(item.get("library_state") or "unknown")
    if library_state == "in_library":
        # 已经入馆：过去那次提交失败（常见是 MoviePilot 提交超时，但种子其实已加入并下完）已不再是待处理的问题。
        return "in_library", [], None
    if item_id in signals.submitted or item_id in signals.external:
        if item_id not in signals.submitted:
            # 已有下载在进行（手动添加的），之前那次提交失败不再是待处理的问题。
            issues = [issue for issue in issues if issue != "submit_failed"]
        if item_id in signals.downloading_active:
            state = signals.transfer_states.get(item_id, "downloading")
            transfer = state if state in {"stalled", "paused", "error"} else "active"
            if item_id in signals.stalled_long:
                issues.append("download_stalled")
        elif signals.transmission_state == "unknown":
            transfer = "unknown"
        else:
            # 已提交但 Transmission 中不在下载：可能已完成、等待 MoviePilot 整理或 Emby 入库。
            transfer = "waiting_library"
            if item_id in signals.organize_failed:
                issues.append("organize_failed")
        return "downloading", issues, transfer
    selected = signals.selected.get(item_id)
    if selected:
        if not any(candidate_id in signals.contexts for candidate_id in selected):
            issues.append("context_expired")
        return "selected", issues, None
    if item_id in signals.active_search:
        return "searching", issues, None
    eligible = signals.eligible.get(item_id) or []
    if eligible:
        if not any(candidate_id in signals.contexts for candidate_id in eligible):
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


def items_with_usable_candidates(items: list[dict[str, Any]]) -> set[int]:
    """这些影片里，哪些还有一个下载上下文没过期的合格候选（过期的候选提交不了，要重新寻片）。"""
    if not items:
        return set()
    with connect() as conn:
        signals = _collect_signals(conn, items)
    return {
        item_id for item_id, candidate_ids in signals.eligible.items()
        if any(candidate_id in signals.contexts for candidate_id in candidate_ids)
    }


async def replaced_stalled_torrents(items: list[dict[str, Any]], live: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """换了资源之后留在 Transmission 里的停滞旧种子：同一部影片已有更晚加入、没停滞的下载。

    ``keep_data`` 为真表示旧种子与新种子是同一个发布名（跨站点的同一资源共用同样的文件），删任务时不能删文件。"""
    with connect() as conn:
        signals = _collect_signals(conn, items)
    await _attach_transmission(signals, {to_int(item["id"]): item for item in items}, live)
    replaced: dict[str, dict[str, Any]] = {}
    for item_id, torrents in signals.active.items():
        newest = signals.newest_healthy.get(item_id)
        if not newest:
            continue
        healthy_names = {
            normalized_download_name(torrent.get("name")) for torrent in torrents
            if not long_stalled(torrent)
        }
        for torrent in torrents:
            torrent_hash = _torrent_hash(torrent)
            if not long_stalled(torrent) or to_int(torrent.get("addedDate")) >= newest or not HASH_PATTERN.fullmatch(torrent_hash):
                continue
            replaced[torrent_hash] = {
                "item_id": item_id, "hash": torrent_hash, "name": str(torrent.get("name") or ""),
                "keep_data": normalized_download_name(torrent.get("name")) in healthy_names,
            }
    return list(replaced.values())


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
        old = conn.execute("SELECT tmdb_id FROM playlist_items WHERE id=?", (item_id,)).fetchone()
        changed = bool(old and old["tmdb_id"] is not None and to_int(old["tmdb_id"]) != to_int(values[0]))
        conn.execute(
            """UPDATE playlist_items
               SET tmdb_id=?,tmdb_title=?,tmdb_original_title=?,tmdb_year=?,tmdb_imdb_id=?,tmdb_checked_at=?,
                   tmdb_poster_path=?,tmdb_original_language=?,fanart_poster_url=NULL,
                   fanart_backdrop_url=NULL,tmdb_backdrop_path=NULL,
                   tmdb_alt_titles_json=CASE WHEN tmdb_id IS ? THEN tmdb_alt_titles_json ELSE NULL END,
                   emby_item_id=NULL,emby_image_tag=NULL,library_state='unknown',library_checked_at=NULL
               WHERE id=?""",
            (*values, tmdb_poster_path(media), tmdb_original_language(media), values[0], item_id),
        )
        stale: list[str] = []
        if changed:
            # 换成了另一部影片：之前为旧影片搜到的候选、选定与下载上下文都不再适用，整部作废，需要重新寻片。
            stale = [row["id"] for row in conn.execute("SELECT id FROM candidates WHERE playlist_item_id=?", (item_id,)).fetchall()]
            conn.execute("DELETE FROM candidates WHERE playlist_item_id=?", (item_id,))
    for candidate_id in stale:
        forget_raw_candidate(candidate_id)


def store_library_state(item_id: int, state: str, emby_item_id: str | None, image_tag: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE playlist_items SET library_state=?,library_checked_at=?,emby_item_id=?,emby_image_tag=? WHERE id=?",
            (state, utc_now(), emby_item_id, image_tag, item_id),
        )
