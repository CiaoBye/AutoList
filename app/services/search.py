"""Search task orchestration, attempts, and follow-up task creation."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from collections import Counter, deque
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
from fastapi import HTTPException

from ..candidate_policy import normalized_policy
from .cookiecloud import with_cookie_refresh
from ..clients import EmbyClient, MTeamClient, NexusPHPClient, RSSClient, TorznabClient, TransmissionClient
from ..schemas import MAX_SEARCH_ITEMS
from ..database import config_values, connect, json_value
from ..logs import event_logger
from ..domain.releases import ReleaseClusters
from ..domain.titles import (
    candidate_identity,
    canonical_item_year,
    is_transmission_downloading,
    normalized_download_name,
    torrent_matches_item,
)
from ..queries.sites import searchable_site_ids
from ..security import safe_error, sanitize_sensitive_text
from ..sites import SiteError
from ..sites.errors import SearchCaptcha
from ..state import forget_raw_candidate, prune_raw_candidates, remember_raw_candidate
from ..tasks import SEARCH
from ..util import first_value, resource_fingerprint, rows_to_dicts, secret_free, to_float, to_int, utc_now
from ..outbound import safe_detail_url
from .library import library_details
from .recognition import analyze_candidate, ensure_alt_titles, persist_tmdb_item, recognize_item


# Transmission 下载列表短 TTL 缓存（审计 3-25）：避免片单每次刷新重复 RPC。
_transmission_snapshot_cache: dict[int, tuple[float, list[dict[str, Any]], str]] = {}
TRANSMISSION_SNAPSHOT_TTL_SECONDS = 10
# 站点访问频率（与 MoviePilot 站点字段语义一致）：limit_interval 秒内最多 limit_count 次请求；
# 只配置 limit_interval 时视为两次请求之间的最小间隔。每个站点一把锁，保证并发任务也按序等待。
_site_request_times: dict[int, deque[float]] = {}
_site_rate_locks: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


# 同一站点相邻两次搜索请求至少间隔的秒数（站点自己配置了更严格的频率时以站点为准）。
SITE_REQUEST_GAP_SECONDS = 3.0
_site_last_request: dict[int, float] = {}


async def wait_for_site_rate_limit(site: dict[str, Any]) -> None:
    site_id = to_int(site.get("id") or 0)
    if not site_id:
        return
    await _wait_for_site_gap(site_id)
    interval = to_float(site.get("limit_interval")) if site.get("limit_interval") is not None else 0.0
    if interval <= 0:
        return
    count = max(1, to_int(site.get("limit_count") or 1))
    loop = asyncio.get_running_loop()
    entry = _site_rate_locks.get(site_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _site_rate_locks[site_id] = entry
    async with entry[1]:
        history = _site_request_times.setdefault(site_id, deque())
        now = time.monotonic()
        while history and now - history[0] >= interval:
            history.popleft()
        if len(history) >= count:
            await asyncio.sleep(max(0.0, interval - (now - history[0])))
            now = time.monotonic()
            while history and now - history[0] >= interval:
                history.popleft()
        history.append(time.monotonic())


async def _wait_for_site_gap(site_id: int) -> None:
    """同一站点的请求排队，相邻两次之间至少隔 SITE_REQUEST_GAP_SECONDS 秒。"""
    loop = asyncio.get_running_loop()
    entry = _site_gap_locks.get(site_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _site_gap_locks[site_id] = entry
    async with entry[1]:
        last = _site_last_request.get(site_id)
        if last is not None:
            remaining = SITE_REQUEST_GAP_SECONDS - (time.monotonic() - last)
            if remaining > 0:
                await asyncio.sleep(remaining)
        _site_last_request[site_id] = time.monotonic()


_site_gap_locks: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def invalidate_downloads_snapshot() -> None:
    _transmission_snapshot_cache.clear()


async def _current_downloads_cached(force: bool = False) -> tuple[list[dict[str, Any]], str]:
    import time as _time

    now = _time.monotonic()
    cached = _transmission_snapshot_cache.get(0)
    if not force and cached is not None and now - cached[0] < TRANSMISSION_SNAPSHOT_TTL_SECONDS:
        return cached[1], cached[2]
    transmission = TransmissionClient()
    try:
        torrents = await asyncio.wait_for(transmission.current_downloads(), timeout=6)
        state = "known_present" if torrents else "known_empty"
    except Exception:
        torrents = []
        state = "unknown"
    _transmission_snapshot_cache[0] = (_time.monotonic(), torrents, state)
    return torrents, state


async def searchable_playlist_items(playlist_id: int, limit: int | None = None) -> dict[str, Any]:
    with connect() as conn:
        playlist = conn.execute("SELECT id,name FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if not playlist:
            raise HTTPException(404, "片单不存在")
        stats = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN library_state='in_library' THEN 1 ELSE 0 END) AS in_library
               FROM playlist_items WHERE playlist_id=?""", (playlist_id,),
        ).fetchone()
        rows = list(conn.execute(
            "SELECT * FROM playlist_items WHERE playlist_id=? AND library_state!='in_library' ORDER BY rank_no",
            (playlist_id,),
        ).fetchall())
        history_rows = conn.execute(
            """SELECT h.torrent_name,h.title,c.title AS candidate_title,
                      COALESCE(h.playlist_item_id,c.playlist_item_id) AS playlist_item_id
               FROM download_history h
               LEFT JOIN candidates c ON c.id=h.candidate_id
               WHERE h.playlist_item_id IN (SELECT id FROM playlist_items WHERE playlist_id=?)
                  OR c.playlist_item_id IN (SELECT id FROM playlist_items WHERE playlist_id=?)""",
            (playlist_id, playlist_id),
        ).fetchall()
        candidate_rows = conn.execute(
            """SELECT NULL AS torrent_name,c.title,c.title AS candidate_title,c.playlist_item_id FROM candidates c
               JOIN playlist_items p ON p.id=c.playlist_item_id
               WHERE p.playlist_id=?""", (playlist_id,),
        ).fetchall()
    torrents, transmission_state = await _current_downloads_cached()
    if transmission_state == "unknown":
        return {
            "playlist_id": playlist_id, "playlist_name": playlist["name"],
            "total_count": to_int(stats["total"] or 0), "in_library_count": to_int(stats["in_library"] or 0),
            "downloading_count": None, "download_state": "unknown", "pending_count": 0, "items": [],
        }
    downloading = [torrent for torrent in torrents if is_transmission_downloading(torrent)]
    download_names = {normalized_download_name(first_value(torrent, ("name", "torrent_name"), "")) for torrent in downloading}
    related_names: dict[str, int] = {}
    for row in list(history_rows) + list(candidate_rows):
        item_id = row["playlist_item_id"]
        for name in (row["torrent_name"], row["title"], row["candidate_title"]):
            normalized = normalized_download_name(name)
            if normalized:
                related_names[normalized] = to_int(item_id)
    downloading_ids: set[int] = set()
    for name in download_names:
        if name in related_names:
            downloading_ids.add(related_names[name])
    for torrent in downloading:
        torrent_title = str(first_value(torrent, ("name", "torrent_name"), ""))
        for item in rows:
            if to_int(item["id"]) not in downloading_ids and torrent_matches_item(item, torrent_title):
                downloading_ids.add(to_int(item["id"]))
    queue = [item for item in rows if to_int(item["id"]) not in downloading_ids]
    safe_limit = min(max(1, to_int(limit)), MAX_SEARCH_ITEMS) if limit is not None else MAX_SEARCH_ITEMS
    selected = queue[:safe_limit]
    return {
        "playlist_id": playlist_id, "playlist_name": playlist["name"],
        "total_count": to_int(stats["total"] or 0), "in_library_count": to_int(stats["in_library"] or 0),
        "downloading_count": len(downloading), "download_state": transmission_state,
        "pending_count": len(queue), "items": rows_to_dicts(selected),
    }


def build_search_queries(item: sqlite3.Row, media: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    """Return a bounded IMDb/title search plan, preserving TMDB as the authority."""
    item_keys = item.keys() if hasattr(item, "keys") else ()
    tmdb_imdb_id = item["tmdb_imdb_id"] if "tmdb_imdb_id" in item_keys else None
    item_imdb_id = item["imdb_id"] if "imdb_id" in item_keys else None
    imdb_id = str(media.get("imdb_id") or tmdb_imdb_id or item_imdb_id or "").strip() or None
    # 种子标题多按首映年份命名，与片单来源年份一致；TMDB 年份可能晚一年（如卡萨布兰卡 1942 / 1943），只作后备。
    source_year = item["year"] if "year" in item_keys else None
    year = str(source_year or media.get("year") or canonical_item_year(item) or "").strip()
    titles = [
        str(media.get("original_title") or "").strip(),
        str(media.get("title") or "").strip(),
        str(item["original_title"] or "").strip(),
        str(item["chinese_title"] or "").strip(),
    ]
    queries: list[tuple[str, str | None, str]] = []
    if imdb_id:
        queries.append((titles[0] or titles[1], imdb_id, f"IMDb {imdb_id}"))
    seen: set[str] = set()
    for title in titles:
        key = re.sub(r"\W+", "", title).casefold()
        if not title or key in seen:
            continue
        seen.add(key)
        keyword = title if year and re.search(rf"(?:^|\D){re.escape(year)}(?:\D|$)", title) else f"{title} {year}".strip()
        queries.append((keyword, None, keyword))
    return queries[:4]

def update_task(task_id: int, **values: Any) -> None:
    if not set(values).issubset({"status", "completed", "matched", "error_message", "done_item_ids_json"}):
        raise ValueError("无效的搜索任务字段")
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in values)
    with connect() as conn:
        # Dynamic column names are restricted by the allowlist above.
        conn.execute(f"UPDATE search_tasks SET {assignments} WHERE id=?", (*values.values(), task_id))  # nosec B608


def task_log(task_id: int, level: str, stage: str, message: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO search_task_logs(task_id,level,stage,message,created_at) VALUES(?,?,?,?,?)",
            (task_id, level, stage, sanitize_sensitive_text(message, 1000), utc_now()),
        )


def begin_search_task_slot(conn: sqlite3.Connection) -> None:
    """Lock task admission and enforce the process-wide persisted capacity."""
    conn.execute("BEGIN IMMEDIATE")
    active = to_int(conn.execute(
        "SELECT COUNT(*) FROM search_tasks WHERE status IN ('queued','running')",
    ).fetchone()[0])
    capacity_rejection = SEARCH.capacity_error(active)
    if capacity_rejection is not None:
        raise HTTPException(429, capacity_rejection)


SEARCH_FILM_CONCURRENCY = 3
# 同一个站点同一时间只发一个搜索请求，避免并发请求被站点限流或判为异常。
SEARCH_SITE_CONCURRENCY = 1
SEARCH_REQUEST_CONCURRENCY = 10
# “仅补缺”站点只为可选种子少于这个数的影片补搜。
SUPPLEMENT_MIN_RELEASES = 3
SITE_BLOCKED = "site_blocked"
SITE_CAPTCHA = "site_captcha"
# 站点要求搜索人机验证后：每 CAPTCHA_PROBE_SECONDS 试一次，最多等 CAPTCHA_WAIT_SECONDS（某站点连续搜索过快触发后约 10 分钟内自行恢复）。
CAPTCHA_PROBE_SECONDS = 60
CAPTCHA_WAIT_SECONDS = 20 * 60
BLOCKING_ERRORS = {SITE_BLOCKED, SITE_CAPTCHA}
SEARCH_ERROR_MESSAGES = {
    "dns_error": "无法解析站点地址，请检查域名或 DNS 设置",
    "connect_error": "无法连接站点，请检查地址和网络",
    "timeout": "站点响应超时，请稍后重试",
    "auth_error": "站点认证失败，请检查登录信息",
    "rate_limit": "站点请求过于频繁，请稍后重试",
    "http_error": "站点返回异常，请稍后重试或检查站点状态",
    "parse_error": "站点返回内容无法解析，请重试或检查站点适配",
    "error": "站点搜索失败，请查看日志后重试",
}


def classify_search_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, SearchCaptcha):
        return SITE_CAPTCHA, str(exc)
    if isinstance(exc, SiteError):
        # 登录失效、二次验证、维护、人机验证：站点自己说明了原因，本次任务里重试也不会好转。
        return SITE_BLOCKED, str(exc)
    technical = safe_error(exc).casefold()
    if isinstance(exc, httpx.TimeoutException) or "timed out" in technical or "timeout" in technical:
        return "timeout", SEARCH_ERROR_MESSAGES["timeout"]
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return "auth_error", SEARCH_ERROR_MESSAGES["auth_error"]
        if status == 429:
            return "rate_limit", SEARCH_ERROR_MESSAGES["rate_limit"]
        return "http_error", f"站点返回 HTTP {status}，请稍后重试"
    if isinstance(exc, httpx.NetworkError) or re.search(
        r"(?:name or service not known|nodename nor servname|temporary failure in name resolution|gaierror)",
        technical,
    ):
        if re.search(r"(?:name or service not known|nodename nor servname|name resolution|gaierror)", technical):
            return "dns_error", SEARCH_ERROR_MESSAGES["dns_error"]
        return "connect_error", SEARCH_ERROR_MESSAGES["connect_error"]
    if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)) or re.search(r"(?:parse|xml|json)", technical):
        return "parse_error", SEARCH_ERROR_MESSAGES["parse_error"]
    return "error", SEARCH_ERROR_MESSAGES["error"]


def mark_site_captcha(site: dict[str, Any], reason: str) -> None:
    """寻片中遇到搜索人机验证时，把站点检测状态也标为异常，站点列表与概览随即提示去验证。"""
    with connect() as conn:
        conn.execute(
            "UPDATE pt_sites SET last_status='error',last_message=?,last_tested_at=? WHERE id=?",
            (reason, utc_now(), site["id"]),
        )


def mark_site_recovered(site: dict[str, Any]) -> None:
    """人机验证解除、搜索恢复后，站点检测状态回到正常，不再提示去验证。"""
    with connect() as conn:
        conn.execute(
            "UPDATE pt_sites SET last_status='ok',last_message=?,last_tested_at=? WHERE id=?",
            ("搜索恢复正常", utc_now(), site["id"]),
        )


def clear_skipped_attempts(task_id: int, item_id: int, site_id: int) -> None:
    """补搜前删掉这部影片在该站点的“跳过 / 需要验证”记录，任务结果只保留补搜的那次。"""
    with connect() as conn:
        conn.execute(
            "DELETE FROM search_attempts WHERE task_id=? AND playlist_item_id=? AND site_id=? AND status='failed' AND error_code=?",
            (task_id, item_id, site_id, SITE_CAPTCHA),
        )


def record_search_attempt(
    task_id: int, item_id: int, site: dict[str, Any], attempt_no: int, status: str,
    result_count: int, duration_ms: int, error_code: str | None = None, error_message: str | None = None,
    query_count: int = 1,
) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO search_attempts(
                 task_id,playlist_item_id,site_id,site_name,attempt_no,status,result_count,duration_ms,
                 error_code,error_message,query_count,created_at,finished_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, item_id, site["id"], site["name"], attempt_no, status, result_count,
             duration_ms, error_code, sanitize_sensitive_text(error_message, 500) if error_message else None,
             query_count, now, now),
        )


# 站点临时出错（HTTP 5xx、429）时等一会儿重试：连续请求过快时部分站点会短暂返回 500，几秒后恢复。
TRANSIENT_RETRY_DELAYS = (3.0, 6.0)


def _is_transient(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code >= 500 or exc.response.status_code == 429
    )


async def _search_with_retry(site: dict[str, Any], client: Any, title: str, imdb_id: str | None) -> list[dict[str, Any]]:
    for delay in (*TRANSIENT_RETRY_DELAYS, None):
        try:
            # Cookie 失效时补拉 CookieCloud 并重试一次；新 Cookie 写回 site，本次任务后续请求直接使用。
            return await with_cookie_refresh(site, lambda current: client.search(current, title, imdb_id))
        except Exception as exc:
            if delay is None or not _is_transient(exc):
                raise
            await asyncio.sleep(delay)
            await wait_for_site_rate_limit(site)
    raise RuntimeError("unreachable")


@asynccontextmanager
async def _search_slot(site_slot: asyncio.Semaphore, shared_slot: asyncio.Semaphore) -> AsyncIterator[None]:
    """先占站点名额，再占全任务名额：慢站点排队时不占用其他站点的请求名额。"""
    async with site_slot:
        async with shared_slot:
            yield


async def search_one_site(
    task_id: int, item: sqlite3.Row, site: dict[str, Any], clients: dict[str, Any],
    semaphore: asyncio.Semaphore | AbstractAsyncContextManager[Any],
    queries: list[tuple[str, str | None, str]], *, is_target: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None, int, str | None]:
    """搜索一个站点，返回（站点、种子、失败原因、检索词数、错误代码）。耗时只计请求本身，不含排队与限速等待。

    第一个检索词（IMDb，站点不支持时为原名）已搜到目标影片时，不再用片名检索：片名检索只为补漏，
    IMDb 命中后再搜通常只是同一批种子，却要多花两三倍的请求。"""
    request_seconds = 0.0
    async with semaphore:
        query_count = 0
        try:
            client = clients.get(str(site["adapter"]))
            if client is None:
                raise RuntimeError(f"不支持的站点适配器：{site['adapter']}")
            unique: dict[str, dict[str, Any]] = {}
            errors: list[Exception] = []
            site_queries = queries[:1] if str(site["adapter"]) == "rss" else queries
            for title, imdb_id, _label in site_queries:
                await wait_for_site_rate_limit(site)
                query_count += 1
                request_started = time.monotonic()
                try:
                    rows = await _search_with_retry(site, client, title, imdb_id)
                except SiteError:
                    # 站点拦截（登录失效、人机验证、维护等）：换检索词也一样，直接报出。
                    request_seconds += time.monotonic() - request_started
                    raise
                except Exception as exc:
                    request_seconds += time.monotonic() - request_started
                    errors.append(exc)
                    continue
                request_seconds += time.monotonic() - request_started
                found_target = False
                for torrent in rows:
                    torrent = dict(torrent)
                    if imdb_id and is_target is not None and not found_target:
                        found_target = is_target(torrent)
                    torrent["_site_priority"] = to_int(site.get("priority") or 100)
                    torrent["_site_id"] = to_int(site["id"])
                    key = str(torrent.get("enclosure") or resource_fingerprint(
                        str(first_value(torrent, ("title", "torrent_name", "name"), "")),
                        first_value(torrent, ("size", "size_bytes")),
                    ))
                    unique[key] = torrent
                if found_target:
                    break
            torrents = list(unique.values())
            if not torrents and errors and len(errors) == query_count:
                raise errors[-1]
            duration_ms = max(0, to_int(request_seconds * 1000))
            record_search_attempt(
                task_id, to_int(item["id"]), site, 1, "success", len(torrents), duration_ms,
                query_count=query_count,
            )
            return site, torrents, None, query_count, None
        except Exception as exc:
            error_code, reason = classify_search_error(exc)
            duration_ms = max(0, to_int(request_seconds * 1000))
            record_search_attempt(
                task_id, to_int(item["id"]), site, 1, "failed", 0, duration_ms, error_code, safe_error(exc),
                query_count=max(1, query_count),
            )
            return site, [], reason, max(1, query_count), error_code


def _snapshot_ids(raw_value: Any, label: str) -> list[int] | None:
    if raw_value is None:
        return None
    try:
        values = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"任务保存的{label}快照无效") from exc
    if not isinstance(values, list) or any(isinstance(value, bool) or not str(value).isdigit() for value in values):
        raise RuntimeError(f"任务保存的{label}快照无效")
    return list(dict.fromkeys(to_int(value) for value in values))


async def run_search(task_id: int) -> None:
    prune_raw_candidates()
    emby, torznab, mteam, nexusphp, rss, config = EmbyClient(), TorznabClient(), MTeamClient(), NexusPHPClient(), RSSClient(), config_values()
    with connect() as conn:
        task = conn.execute("SELECT * FROM search_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return
    if task["status"] not in {"queued", "running"}:
        return
    pair_scope: set[tuple[int, int]] = set()
    try:
        selected_item_ids = _snapshot_ids(task["item_ids_json"], "影片")
        selected_site_ids = _snapshot_ids(task["site_ids_json"], "站点")
        raw_pairs = json.loads(task["pair_scope_json"] or "[]")
        if not isinstance(raw_pairs, list):
            raise ValueError
        pair_scope = {
            (to_int(pair[0]), to_int(pair[1]))
            for pair in raw_pairs
            if isinstance(pair, list) and len(pair) == 2
        }
        with connect() as conn:
            if selected_item_ids is None:
                items = list(conn.execute(
                    "SELECT * FROM playlist_items WHERE playlist_id=? AND rank_no BETWEEN ? AND ? ORDER BY rank_no",
                    (task["playlist_id"], task["range_start"], task["range_end"]),
                ).fetchall())
            else:
                if not selected_item_ids:
                    raise RuntimeError("任务快照中没有影片")
                item_placeholders = ",".join("?" for _ in selected_item_ids)
                items = list(conn.execute(
                    f"SELECT * FROM playlist_items WHERE playlist_id=? AND id IN ({item_placeholders}) ORDER BY rank_no",  # nosec B608
                    (task["playlist_id"], *selected_item_ids),
                ).fetchall())
                missing_items = set(selected_item_ids) - {to_int(item["id"]) for item in items}
                if missing_items:
                    raise RuntimeError(f"任务快照中的影片已不存在：{', '.join(str(value) for value in sorted(missing_items))}")
            if selected_site_ids is None:
                sites = rows_to_dicts(conn.execute(
                    "SELECT * FROM pt_sites WHERE enabled=1 AND search_enabled=1 ORDER BY priority,id",
                ).fetchall())
            else:
                if not selected_site_ids:
                    raise RuntimeError("任务快照中没有搜索站点")
                site_placeholders = ",".join("?" for _ in selected_site_ids)
                site_rows = conn.execute(
                    f"SELECT * FROM pt_sites WHERE id IN ({site_placeholders}) ORDER BY priority,id",  # nosec B608
                    selected_site_ids,
                ).fetchall()
                sites = rows_to_dicts(site_rows)
                missing_sites = set(selected_site_ids) - {to_int(site["id"]) for site in site_rows}
                if missing_sites:
                    raise RuntimeError(f"任务快照中的站点已不存在：{', '.join(str(value) for value in sorted(missing_sites))}")
            if not items:
                raise RuntimeError("任务快照中没有可搜索影片")
            if not sites:
                raise RuntimeError("没有可用的搜索站点")
    except Exception as exc:
        reason = safe_error(exc)
        update_task(task_id, status="failed", error_message=reason)
        task_log(task_id, "error", "snapshot", f"搜索快照校验失败：{reason}")
        return
    update_task(task_id, status="running")
    task_log(task_id, "info", "task", f"开始搜索，共 {len(items)} 部影片、{len(sites)} 个搜索来源")
    matched = 0
    warnings: list[str] = []
    # 本次任务中已被站点拦截（登录失效、人机验证、维护等）的站点：后续影片不再请求，直接记为失败，便于处理后“重试失败的站点”。
    blocked_sites: dict[int, tuple[str, str]] = {}
    # 多部影片同时寻片，但同一个站点同一时间只发一个请求、相邻两次至少间隔 SITE_REQUEST_GAP_SECONDS 秒
    # （见 wait_for_site_rate_limit）；每部片搜完立即写入候选并记入 done_item_ids_json，挑选台随即可以看到。
    # “仅补缺”的站点在其他站点搜完后，只为可选资源不足 SUPPLEMENT_MIN_RELEASES 个的影片按 IMDb 检索一次。
    film_slots = asyncio.Semaphore(SEARCH_FILM_CONCURRENCY)
    request_slots = asyncio.Semaphore(SEARCH_REQUEST_CONCURRENCY)
    site_slots = {to_int(site["id"]): asyncio.Semaphore(SEARCH_SITE_CONCURRENCY) for site in sites}
    main_sites = [site for site in sites if not to_int(site.get("supplement_only"))]
    supplement_sites = [site for site in sites if to_int(site.get("supplement_only"))]
    clients = {"torznab": torznab, "mteam": mteam, "nexusphp": nexusphp, "rss": rss}
    try:
        policy = normalized_policy(json.loads(config.get("candidate_policy") or "{}"))
    except (TypeError, json.JSONDecodeError):
        policy = normalized_policy({})
    done_item_ids: list[int] = []
    kept_by_film: dict[int, int] = {}
    # 因搜索人机验证暂停的站点 → 还没在该站搜过的影片（与当时用的检索词），任务最后等站点恢复后补搜。
    captcha_waiting: dict[int, list[tuple[dict[str, Any], list[tuple[str, str | None, str]]]]] = {}

    async def prepare_film(item: sqlite3.Row) -> dict[str, Any] | None:
        """识别、核对 Emby；已入馆返回 None。"""
        label = f"#{item['rank_no']} {item['original_title']}"
        task_log(task_id, "info", "recognize", f"开始识别 {label}")
        tmdb_media = await recognize_item(item)
        if not tmdb_media:
            raise RuntimeError("TMDB 未返回匹配结果")
        tmdb_id = to_int(tmdb_media["id"])
        media = {
            "source": "themoviedb",
            "tmdb_id": tmdb_id,
            "imdb_id": tmdb_media.get("imdb_id") or item["imdb_id"],
            "title": tmdb_media.get("title") or item["chinese_title"] or item["original_title"],
            "original_title": tmdb_media.get("original_title") or item["original_title"],
            "year": str(tmdb_media.get("release_date") or item["year"] or "")[:4] or None,
            "release_date": tmdb_media.get("release_date"),
            "type": "电影",
            "poster_path": tmdb_media.get("poster_path"),
        }
        persist_tmdb_item(to_int(item["id"]), tmdb_media, item["imdb_id"])
        # sqlite3.Row is a snapshot: use the just-recognized identity
        # for this search's candidate filtering as well as future runs.
        with connect() as conn:
            item = conn.execute("SELECT * FROM playlist_items WHERE id=?", (item["id"],)).fetchone()
        if item is None:
            raise RuntimeError("搜索中的影片已不存在")
        item = await ensure_alt_titles(item)
        task_log(task_id, "info", "recognize", f"识别完成 {label} → TMDB {tmdb_id}")
        state, emby_item_id, image_tag = await library_details(
            emby, str(media["title"]), to_int(media["year"]) if media.get("year") else item["year"],
            tmdb_id, media.get("imdb_id"),
        )
        with connect() as conn:
            conn.execute(
                "UPDATE playlist_items SET library_state=?,library_checked_at=?,emby_item_id=?,emby_image_tag=? WHERE id=?",
                (state, utc_now(), emby_item_id, image_tag, item["id"]),
            )
        task_log(task_id, "info", "library", f"Emby 状态：{state}")
        if state == "in_library":
            task_log(task_id, "info", "search", f"{label} 已有实体文件，跳过站点搜索")
            return None
        queries = build_search_queries(item, media)
        task_log(task_id, "info", "search", "检索词：" + " → ".join(query[2] for query in queries))
        return {
            "item": item, "media": media, "tmdb_id": tmdb_id, "state": state, "label": label,
            "queries": queries, "pairs": [], "site_failures": 0,
        }

    async def search_sites(film: dict[str, Any], film_sites: list[dict[str, Any]], queries: list[tuple[str, str | None, str]]) -> None:
        """在一组站点上搜索这部影片，结果累加到 film["pairs"]。"""
        item, media, label = film["item"], film["media"], film["label"]

        def is_target(torrent: dict[str, Any]) -> bool:
            return candidate_identity(
                item, media, str(first_value(torrent, ("title", "torrent_name", "name"), "")),
                first_value(torrent, ("imdbid", "imdb_id")),
            )[0]

        item_sites = [
            site for site in film_sites
            if not pair_scope or (to_int(item["id"]), to_int(site["id"])) in pair_scope
        ]
        for site in item_sites:
            if to_int(site["id"]) in blocked_sites:
                film["site_failures"] += 1
                record_search_attempt(
                    task_id, to_int(item["id"]), site, 1, "failed", 0, 0, blocked_sites[to_int(site["id"])][0],
                    f"本次任务已跳过：{blocked_sites[to_int(site['id'])][1]}", query_count=0,
                )
                if blocked_sites[to_int(site["id"])][0] == SITE_CAPTCHA:
                    captcha_waiting.setdefault(to_int(site["id"]), []).append((film, queries))
        site_results = await asyncio.gather(*(
            search_one_site(
                task_id, item, site, clients, _search_slot(site_slots[to_int(site["id"])], request_slots), queries,
                is_target=is_target,
            )
            for site in item_sites if to_int(site["id"]) not in blocked_sites
        ))
        for site, torrents, reason, query_count, error_code in site_results:
            if reason:
                film["site_failures"] += 1
                if error_code in BLOCKING_ERRORS:
                    # 并行的几部影片可能同时撞上同一个被拦截的站点，警告只记一次。
                    paused = error_code == SITE_CAPTCHA
                    note = "暂停该站点，其他站点搜完后等待恢复再补搜" if paused else "本次任务后续影片跳过该站点"
                    if to_int(site["id"]) not in blocked_sites:
                        warnings.append(f"{site['name']}：{reason}（{note}）")
                        if paused:
                            mark_site_captcha(site, reason)
                    blocked_sites[to_int(site["id"])] = (error_code, reason)
                    if paused:
                        captcha_waiting.setdefault(to_int(site["id"]), []).append((film, queries))
                    task_log(task_id, "warning", "search", f"{site['name']} 搜索失败：{reason}；{note}")
                else:
                    warnings.append(f"{label} · {site['name']}：{reason}")
                    task_log(task_id, "warning", "search", f"{site['name']} 搜索失败：{reason}")
                event_logger().warning("site_search_failed", extra={
                    "task_id": task_id, "rank": item["rank_no"], "movie": item["original_title"],
                    "site": site["name"], "error": reason[:300],
                })
                continue
            film["pairs"].extend((media, torrent) for torrent in torrents)
            task_log(task_id, "info", "search", f"{site['name']} 返回 {len(torrents)} 个资源（{query_count} 个检索词）")

    def save_candidates(film: dict[str, Any]) -> int:
        """按入馆标准挑出候选并写入（补缺后整部重写），返回保留的可选种子数。"""
        item, label, state, tmdb_id = film["item"], film["label"], film["state"], film["tmdb_id"]
        media, site_failures = film["media"], film["site_failures"]
        with connect() as conn:
            # 用户已选进待入馆清单的资源整行保留（选择表随候选级联删除），重写时只替换其余候选。
            rows = conn.execute(
                """SELECT c.id, c.site_name, c.resource_key, s.candidate_id IS NOT NULL AS picked FROM candidates c
                   LEFT JOIN selection_items s ON s.candidate_id=c.id WHERE c.task_id=? AND c.playlist_item_id=?""",
                (task_id, item["id"]),
            ).fetchall()
            stale = [row["id"] for row in rows if not row["picked"]]
            picked_keys = {(row["site_name"], row["resource_key"]) for row in rows if row["picked"]}
            if stale:
                conn.executemany("DELETE FROM candidates WHERE id=?", [(candidate_id,) for candidate_id in stale])
        for candidate_id in stale:
            forget_raw_candidate(candidate_id)
        pairs = sorted(film["pairs"], key=lambda pair: analyze_candidate(str(first_value(pair[1], ("title", "torrent_name", "name"), "")), 0, config, pair[1], policy)["ranking"])
        limit = to_int(policy["candidate_limit"])
        eligible_keys: list[str] = []
        excluded_keys: list[str] = []
        selected_pairs: list[tuple[dict[str, Any] | None, dict[str, Any]]] = []
        selected_keys: set[tuple[str, str, str]] = set()
        exclusion_counts: Counter[str] = Counter()
        releases = ReleaseClusters()
        for pair in pairs:
            torrent = pair[1]
            analysis = analyze_candidate(
                str(first_value(torrent, ("title", "torrent_name", "name"), "")), 0, config, torrent, policy,
            )
            identity_ok, identity_reason = candidate_identity(
                item, media, str(first_value(torrent, ("title", "torrent_name", "name"), "")),
                first_value(torrent, ("imdbid", "imdb_id")),
            )
            if not identity_ok:
                analysis = dict(analysis)
                analysis.update({
                    "eligible": False, "manual": True, "recommendation": "excluded",
                    "reason": identity_reason, "exclusion_reason": identity_reason,
                })
            if not analysis["eligible"]:
                exclusion_counts[str(analysis.get("exclusion_reason") or "不符合允许组合")] += 1
            # 保留数量按不同发布计：同一种子在多个站点、标题写法不同，只占一个名额。
            key = releases.key(
                str(first_value(torrent, ("title", "torrent_name", "name"), "")),
                first_value(torrent, ("size", "size_bytes")), analysis.get("group"), analysis.get("resolution"),
            )
            bucket_name = "eligible" if analysis["eligible"] else "excluded"
            bucket = eligible_keys if analysis["eligible"] else excluded_keys
            if key not in bucket:
                if len(bucket) >= limit:
                    continue
                bucket.append(key)
            selection_key = (bucket_name, key, str(first_value(torrent, ("site_name", "site"), torrent.get("_site_id") or "")))
            if selection_key in selected_keys:
                continue
            selected_keys.add(selection_key)
            selected_pairs.append(pair)
        kept = len(eligible_keys)
        summary = {
            "rank": item["rank_no"], "movie": item["original_title"],
            "results": len(pairs), "kept": kept,
            "excluded": [{"reason": reason, "count": count} for reason, count in exclusion_counts.most_common(3)],
        }
        if site_failures:
            summary["site_failures"] = site_failures
        task_log(task_id, "info", "summary", json.dumps(summary, ensure_ascii=False))
        event_logger().info("movie_search_summary", extra={
            "task_id": task_id, "rank": item["rank_no"], "movie": item["original_title"],
            "results": len(pairs), "kept": kept, "excluded": len(excluded_keys),
        })
        # 候选写入合并为单个事务（审计 3-29）：避免每候选独立 SQLite 事务的写放大。
        candidate_rows: list[tuple[Any, ...]] = []
        for index, (source_media, torrent) in enumerate(selected_pairs):
            candidate_id = uuid.uuid4().hex
            title = str(first_value(torrent, ("title", "torrent_name", "name"), "未知资源"))
            analyzed = analyze_candidate(title, index, config, torrent, policy)
            identity_ok, identity_reason = candidate_identity(
                item, source_media, title, first_value(torrent, ("imdbid", "imdb_id")),
            )
            if not identity_ok:
                analyzed = dict(analyzed)
                analyzed.update({
                    "eligible": False, "manual": True, "recommendation": "excluded",
                    "reason": identity_reason, "exclusion_reason": identity_reason,
                })
            metadata = secret_free({
                "description": torrent.get("description"), "labels": torrent.get("labels", []),
                "volume_factor": torrent.get("volume_factor"), "publish_time": first_value(torrent, ("pubdate", "publish_time")),
                "source": analyzed["source"], "profile_label": analyzed.get("profile_label"),
            })
            fingerprint = resource_fingerprint(title, first_value(torrent, ("size", "size_bytes")))
            if (first_value(torrent, ("site_name", "site")), fingerprint) in picked_keys:
                continue
            candidate_rows.append((
                candidate_id, task_id, item["id"], index, title, first_value(torrent, ("site_name", "site")),
                first_value(torrent, ("size", "size_bytes")), first_value(torrent, ("seeders", "seeder")), analyzed["resolution"],
                analyzed["codec"], analyzed["group"], analyzed["tier"], analyzed["score"], json_value(analyzed["breakdown"]),
                analyzed["ranking"], analyzed["recommendation"], analyzed["reason"], fingerprint, state,
                to_int(analyzed["manual"]), "eligible" if analyzed["eligible"] else "excluded",
                analyzed.get("exclusion_reason"), analyzed.get("profile_id"), safe_detail_url(first_value(torrent, ("detail_url",))), json_value(metadata), utc_now()),
            )
            remember_raw_candidate(candidate_id, {"media": source_media, "torrent": torrent, "tmdb_id": tmdb_id})
        if candidate_rows:
            with connect() as conn:
                conn.executemany(
                    """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,size,seeders,resolution,codec,group_name,group_tier,score,score_breakdown,ranking,recommendation,recommendation_reason,resource_key,library_state,is_manual_only,eligibility,exclusion_reason,profile_id,detail_url,metadata_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    candidate_rows,
                )
        task_log(
            task_id, "info", "candidate",
            f"{label} 保留 {len(eligible_keys)} 个可下载候选，记录 {len(excluded_keys)} 个排除样本",
        )
        return len(eligible_keys)

    async def process(item: sqlite3.Row) -> dict[str, Any] | None:
        """主站点搜索；需要补缺时返回这部影片的上下文，留到补缺阶段。"""
        nonlocal matched
        film: dict[str, Any] | None = None
        async with film_slots:
            try:
                film = await prepare_film(item)
                if film is not None:
                    await search_sites(film, main_sites, film["queries"])
                    kept_by_film[to_int(item["id"])] = save_candidates(film)
                    matched += kept_by_film[to_int(item["id"])]
            except Exception as exc:
                reason = safe_error(exc)
                label = f"#{item['rank_no']} {item['original_title']}"
                warnings.append(f"{label}：{reason}")
                task_log(task_id, "error", "movie", f"{label} 处理失败：{reason}")
                film = None
        # 重试任务里补缺站点的组合都是上次失败的，照常补搜；否则只补可选资源不足的影片。
        if film is not None and supplement_sites and (pair_scope or kept_by_film[to_int(item["id"])] < SUPPLEMENT_MIN_RELEASES):
            return film
        finish(item)
        return None

    async def supplement(film: dict[str, Any]) -> None:
        nonlocal matched
        item = film["item"]
        async with film_slots:
            try:
                task_log(task_id, "info", "search", f"{film['label']} 可选资源不足 {SUPPLEMENT_MIN_RELEASES} 个，按 IMDb 补搜仅补缺站点")
                # 补缺只用第一个检索词（IMDb，影片没有 IMDb 编号时为原名），节省站点的搜索次数。
                await search_sites(film, supplement_sites, film["queries"][:1])
                previous = kept_by_film.get(to_int(item["id"]), 0)
                kept_by_film[to_int(item["id"])] = save_candidates(film)
                matched += kept_by_film[to_int(item["id"])] - previous
            except Exception as exc:
                reason = safe_error(exc)
                warnings.append(f"{film['label']}：{reason}")
                task_log(task_id, "error", "movie", f"{film['label']} 补缺失败：{reason}")
        finish(item)

    async def resume_captcha_site(site_id: int, waiting: list[tuple[dict[str, Any], list[tuple[str, str | None, str]]]]) -> None:
        """站点要求搜索人机验证时不放弃：其他站点搜完后每分钟试一次（最多 CAPTCHA_WAIT_SECONDS），
        恢复后（冷却结束，或用户在浏览器完成了验证）接着为这些影片补搜。"""
        nonlocal matched
        site = next(site for site in sites if to_int(site["id"]) == site_id)
        first_film, first_queries = waiting[0]
        minutes = CAPTCHA_WAIT_SECONDS // 60
        task_log(
            task_id, "warning", "search",
            f"{site['name']} 需要搜索人机验证：{len(waiting)} 部影片等待该站点，每分钟试一次，最多等 {minutes} 分钟；"
            "可在浏览器打开该站种子列表页完成验证，恢复后自动继续",
        )
        deadline = time.monotonic() + CAPTCHA_WAIT_SECONDS
        while True:
            await asyncio.sleep(CAPTCHA_PROBE_SECONDS)
            await wait_for_site_rate_limit(site)
            try:
                client = clients[str(site["adapter"])]
                title, imdb_id, _label = first_queries[0]
                await _search_with_retry(site, client, title, imdb_id)
                break
            except SearchCaptcha:
                if time.monotonic() >= deadline:
                    task_log(task_id, "warning", "search", f"{site['name']} 等了 {minutes} 分钟仍需验证，这些影片留待“重试失败的站点”")
                    return
            except Exception as exc:
                task_log(task_id, "warning", "search", f"{site['name']} 恢复检查失败：{safe_error(exc)}，留待“重试失败的站点”")
                return
        task_log(task_id, "info", "search", f"{site['name']} 已恢复，继续为 {len(waiting)} 部影片搜索")
        blocked_sites.pop(site_id, None)
        mark_site_recovered(site)
        warnings[:] = [warning for warning in warnings if not warning.startswith(f"{site['name']}：")]
        for film, queries in waiting:
            if site_id in blocked_sites:
                break
            item_id = to_int(film["item"]["id"])
            clear_skipped_attempts(task_id, item_id, site_id)
            film["site_failures"] = max(0, film["site_failures"] - 1)
            await search_sites(film, [site], queries)
            previous = kept_by_film.get(item_id, 0)
            kept_by_film[item_id] = save_candidates(film)
            matched += kept_by_film[item_id] - previous
        update_task(task_id, matched=matched)

    def finish(item: sqlite3.Row) -> None:
        done_item_ids.append(to_int(item["id"]))
        update_task(task_id, completed=len(done_item_ids), matched=matched, done_item_ids_json=json_value(done_item_ids))

    try:
        pending = [film for film in await asyncio.gather(*(process(item) for item in items)) if film is not None]
        if pending:
            task_log(task_id, "info", "search", f"其他站点搜完，{len(pending)} 部影片补搜仅补缺站点")
            await asyncio.gather(*(supplement(film) for film in pending))
        # 需要人机验证的站点：等它恢复后补搜（不同站点同时等）。
        if captcha_waiting:
            await asyncio.gather(*(resume_captcha_site(site_id, waiting) for site_id, waiting in list(captcha_waiting.items())))
        status = "partial" if warnings else "completed"
        message = "；".join(warnings[:5])[:500] if warnings else None
        update_task(task_id, status=status, completed=len(items), matched=matched, error_message=message)
        event_logger().info("search_task_finished", extra={
            "task_id": task_id, "status": status, "total": len(items),
            "completed": len(items), "matched": matched,
        })
        task_log(task_id, "warning" if warnings else "info", "task", f"任务结束：{len(items)} 部已处理，{matched} 个候选，{len(warnings)} 个警告")
    except asyncio.CancelledError:
        update_task(task_id, status="cancelled")
        raise
    except Exception as exc:
        reason = safe_error(exc)
        update_task(task_id, status="failed", error_message=reason)
        task_log(task_id, "error", "task", f"任务异常停止：{reason}")

def create_followup_search_task(task_id: int, failed_only: bool) -> tuple[int, int]:
    with connect() as conn:
        source = conn.execute("SELECT * FROM search_tasks WHERE id=?", (task_id,)).fetchone()
        if not source:
            raise HTTPException(404, "搜索任务不存在")
        if source["status"] in {"queued", "running"}:
            raise HTTPException(409, "任务仍在执行，完成后才能重试或重新搜索")
        begin_search_task_slot(conn)
        source = conn.execute("SELECT * FROM search_tasks WHERE id=?", (task_id,)).fetchone()
        if not source:
            raise HTTPException(404, "搜索任务不存在")
        site_ids: list[int] = []
        item_ids: list[int] = []
        pair_scope: list[list[int]] = []
        # 重试与重新开始都按当前“参与搜索”的站点：原任务之后停用的站点不再搜索。
        searchable = set(searchable_site_ids(conn))
        if failed_only:
            all_failed = conn.execute(
                """SELECT DISTINCT site_id,playlist_item_id FROM search_attempts
                   WHERE task_id=? AND status='failed' AND site_id IS NOT NULL""", (task_id,),
            ).fetchall()
            if not all_failed:
                raise HTTPException(422, "该任务没有可重试的站点失败记录")
            failed = [row for row in all_failed if to_int(row["site_id"]) in searchable]
            if not failed:
                raise HTTPException(422, "失败的站点都已不参与搜索，无需重试")
            site_ids = sorted({to_int(row["site_id"]) for row in failed})
            item_ids = sorted({to_int(row["playlist_item_id"]) for row in failed})
            pair_scope = sorted([
                [to_int(row["playlist_item_id"]), to_int(row["site_id"])]
                for row in failed
            ])
        else:
            if not searchable:
                raise HTTPException(422, "没有参与搜索的站点，请先在设置中开启")
            # 不记站点快照：开始搜索时读取当时参与搜索的站点。
            try:
                item_ids = [to_int(value) for value in json.loads(source["item_ids_json"] or "[]")]
                source_pairs = json.loads(source["pair_scope_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                item_ids, source_pairs = [], []
            if source_pairs:
                # 原任务是“重试失败的站点”：只保留仍参与搜索的站点组合。
                pair_scope = [
                    pair for pair in source_pairs
                    if isinstance(pair, list) and len(pair) == 2 and to_int(pair[1]) in searchable
                ]
                if not pair_scope:
                    raise HTTPException(422, "这次重试涉及的站点都已不参与搜索")
                site_ids = sorted({to_int(pair[1]) for pair in pair_scope})
        total = len(item_ids) if item_ids else to_int(source["total"])
        now = utc_now()
        new_id = conn.execute(
            """INSERT INTO search_tasks(
                 playlist_id,range_start,range_end,status,total,parent_task_id,trigger,site_ids_json,item_ids_json,
                 pair_scope_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source["playlist_id"], source["range_start"], source["range_end"], "queued", total, task_id,
             "retry" if failed_only else "restart", json_value(site_ids) if site_ids else None,
             json_value(item_ids) if item_ids else None, json_value(pair_scope) if pair_scope else None, now, now),
        ).lastrowid
    return to_int(new_id), total
