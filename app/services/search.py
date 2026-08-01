"""Search task orchestration, attempts, and follow-up task creation."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from typing import Any

import httpx
from fastapi import HTTPException

from ..candidate_policy import normalized_policy
from ..clients import EmbyClient, MTeamClient, NexusPHPClient, RSSClient, TorznabClient, TransmissionClient
from ..database import config_values, connect, json_value
from ..domain.titles import (
    candidate_identity,
    canonical_item_year,
    is_transmission_downloading,
    normalized_download_name,
    torrent_matches_item,
)
from ..security import safe_error, sanitize_sensitive_text
from ..state import (
    enforce_search_task_capacity,
    prune_raw_candidates,
    remember_raw_candidate,
    running_tasks,
)
from ..util import first_value, resource_fingerprint, rows_to_dicts, secret_free, utc_now
from .library import library_details
from .recognition import analyze_candidate, persist_tmdb_item, recognize_movie


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
    try:
        torrents = await asyncio.wait_for(TransmissionClient().current_downloads(), timeout=6)
    except Exception:
        torrents = []
    downloading = [torrent for torrent in torrents if is_transmission_downloading(torrent)]
    download_names = {normalized_download_name(first_value(torrent, ("name", "torrent_name"), "")) for torrent in downloading}
    related_names: dict[str, int] = {}
    for row in list(history_rows) + list(candidate_rows):
        item_id = row["playlist_item_id"]
        for name in (row["torrent_name"], row["title"], row["candidate_title"]):
            normalized = normalized_download_name(name)
            if normalized:
                related_names[normalized] = int(item_id)
    downloading_ids: set[int] = set()
    for name in download_names:
        if name in related_names:
            downloading_ids.add(related_names[name])
    for torrent in downloading:
        torrent_title = str(first_value(torrent, ("name", "torrent_name"), ""))
        for item in rows:
            if int(item["id"]) not in downloading_ids and torrent_matches_item(item, torrent_title):
                downloading_ids.add(int(item["id"]))
    queue = [item for item in rows if int(item["id"]) not in downloading_ids]
    selected = queue[:limit] if limit is not None else queue
    return {
        "playlist_id": playlist_id, "playlist_name": playlist["name"],
        "total_count": int(stats["total"] or 0), "in_library_count": int(stats["in_library"] or 0),
        "downloading_count": len(downloading),
        "pending_count": len(queue), "items": rows_to_dicts(selected),
    }


def build_search_queries(item: sqlite3.Row, media: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    """Return a bounded IMDb/title search plan, preserving TMDB as the authority."""
    item_keys = item.keys() if hasattr(item, "keys") else ()
    tmdb_imdb_id = item["tmdb_imdb_id"] if "tmdb_imdb_id" in item_keys else None
    item_imdb_id = item["imdb_id"] if "imdb_id" in item_keys else None
    imdb_id = str(media.get("imdb_id") or tmdb_imdb_id or item_imdb_id or "").strip() or None
    year = str(media.get("year") or canonical_item_year(item) or "").strip()
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
    if not set(values).issubset({"status", "completed", "matched", "error_message"}):
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
    active = int(conn.execute(
        "SELECT COUNT(*) FROM search_tasks WHERE status IN ('queued','running')",
    ).fetchone()[0])
    enforce_search_task_capacity(active)


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


async def search_one_site(
    task_id: int, item: sqlite3.Row, site: dict[str, Any], clients: dict[str, Any], semaphore: asyncio.Semaphore,
    queries: list[tuple[str, str | None, str]],
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None, int]:
    started = time.monotonic()
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
                query_count += 1
                try:
                    rows = await client.search(site, title, imdb_id)
                except Exception as exc:
                    errors.append(exc)
                    continue
                for torrent in rows:
                    torrent = dict(torrent)
                    torrent["_site_priority"] = int(site.get("priority") or 100)
                    torrent["_site_id"] = int(site["id"])
                    key = str(torrent.get("enclosure") or resource_fingerprint(
                        str(first_value(torrent, ("title", "torrent_name", "name"), "")),
                        first_value(torrent, ("size", "size_bytes")),
                    ))
                    unique[key] = torrent
            torrents = list(unique.values())
            if not torrents and errors and len(errors) == query_count:
                raise errors[-1]
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            record_search_attempt(
                task_id, int(item["id"]), site, 1, "success", len(torrents), duration_ms,
                query_count=query_count,
            )
            return site, torrents, None, query_count
        except Exception as exc:
            error_code, reason = classify_search_error(exc)
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            record_search_attempt(
                task_id, int(item["id"]), site, 1, "failed", 0, duration_ms, error_code, safe_error(exc),
                query_count=max(1, query_count),
            )
            return site, [], reason, max(1, query_count)


def _snapshot_ids(raw_value: Any, label: str) -> list[int] | None:
    if raw_value is None:
        return None
    try:
        values = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"任务保存的{label}快照无效") from exc
    if not isinstance(values, list) or any(isinstance(value, bool) or not str(value).isdigit() for value in values):
        raise RuntimeError(f"任务保存的{label}快照无效")
    return list(dict.fromkeys(int(value) for value in values))


async def run_search(task_id: int) -> None:
    prune_raw_candidates()
    emby, torznab, mteam, nexusphp, rss, config = EmbyClient(), TorznabClient(), MTeamClient(), NexusPHPClient(), RSSClient(), config_values()
    with connect() as conn:
        task = conn.execute("SELECT * FROM search_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return
    if task["status"] not in {"queued", "running"}:
        running_tasks.pop(task_id, None)
        return
    pair_scope: set[tuple[int, int]] = set()
    try:
        selected_item_ids = _snapshot_ids(task["item_ids_json"], "影片")
        selected_site_ids = _snapshot_ids(task["site_ids_json"], "站点")
        raw_pairs = json.loads(task["pair_scope_json"] or "[]")
        if not isinstance(raw_pairs, list):
            raise ValueError
        pair_scope = {
            (int(pair[0]), int(pair[1]))
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
                missing_items = set(selected_item_ids) - {int(item["id"]) for item in items}
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
                missing_sites = set(selected_site_ids) - {int(site["id"]) for site in site_rows}
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
        running_tasks.pop(task_id, None)
        return
    update_task(task_id, status="running")
    task_log(task_id, "info", "task", f"开始搜索，共 {len(items)} 部影片、{len(sites)} 个搜索来源")
    matched = 0
    warnings: list[str] = []
    try:
        for completed, item in enumerate(items, start=1):
            label = f"#{item['rank_no']} {item['original_title']}"
            task_log(task_id, "info", "recognize", f"开始识别 {label}")
            try:
                tmdb_media = await recognize_movie(item["original_title"], item["year"], item["imdb_id"])
                if not tmdb_media:
                    raise RuntimeError("TMDB 未返回匹配结果")
                tmdb_id = int(tmdb_media["id"])
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
                persist_tmdb_item(int(item["id"]), tmdb_media, item["imdb_id"])
                task_log(task_id, "info", "recognize", f"识别完成 {label} → TMDB {tmdb_id}")
                state, emby_item_id, image_tag = await library_details(
                    emby, str(media["title"]), int(media["year"]) if media.get("year") else item["year"],
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
                    update_task(task_id, completed=completed, matched=matched)
                    continue
                pairs: list[tuple[dict[str, Any] | None, dict[str, Any]]] = []
                clients = {"torznab": torznab, "mteam": mteam, "nexusphp": nexusphp, "rss": rss}
                site_semaphore = asyncio.Semaphore(4)
                queries = build_search_queries(item, media)
                task_log(task_id, "info", "search", "检索词：" + " → ".join(query[2] for query in queries))
                item_sites = [
                    site for site in sites
                    if not pair_scope or (int(item["id"]), int(site["id"])) in pair_scope
                ]
                site_results = await asyncio.gather(*(
                    search_one_site(task_id, item, site, clients, site_semaphore, queries) for site in item_sites
                ))
                for site, torrents, reason, query_count in site_results:
                    if reason:
                        warnings.append(f"{label} · {site['name']}：{reason}")
                        task_log(task_id, "warning", "search", f"{site['name']} 搜索失败：{reason}")
                        continue
                    pairs.extend((media, torrent) for torrent in torrents)
                    task_log(task_id, "info", "search", f"{site['name']} 返回 {len(torrents)} 个资源（{query_count} 个检索词）")
                pairs.sort(key=lambda pair: analyze_candidate(str(first_value(pair[1], ("title", "torrent_name", "name"), "")), 0, config, pair[1])["ranking"])
                try:
                    policy = normalized_policy(json.loads(config.get("candidate_policy") or "{}"))
                except (TypeError, json.JSONDecodeError):
                    policy = normalized_policy({})
                limit = int(policy["candidate_limit"])
                eligible_keys: list[str] = []
                excluded_keys: list[str] = []
                selected_pairs: list[tuple[dict[str, Any] | None, dict[str, Any]]] = []
                selected_keys: set[tuple[str, str, str]] = set()
                for pair in pairs:
                    torrent = pair[1]
                    analysis = analyze_candidate(
                        str(first_value(torrent, ("title", "torrent_name", "name"), "")), 0, config, torrent,
                    )
                    identity_ok, identity_reason = candidate_identity(
                        item, media, str(first_value(torrent, ("title", "torrent_name", "name"), "")),
                    )
                    if not identity_ok:
                        analysis = dict(analysis)
                        analysis.update({
                            "eligible": False, "manual": True, "recommendation": "excluded",
                            "reason": identity_reason, "exclusion_reason": identity_reason,
                        })
                    key = resource_fingerprint(
                        str(first_value(torrent, ("title", "torrent_name", "name"), "")),
                        first_value(torrent, ("size", "size_bytes")),
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
                for index, (source_media, torrent) in enumerate(selected_pairs):
                    candidate_id = uuid.uuid4().hex
                    title = str(first_value(torrent, ("title", "torrent_name", "name"), "未知资源"))
                    analyzed = analyze_candidate(title, index, config, torrent)
                    identity_ok, identity_reason = candidate_identity(item, source_media, title)
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
                    with connect() as conn:
                        conn.execute(
                            """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,size,seeders,resolution,codec,group_name,group_tier,score,score_breakdown,ranking,recommendation,recommendation_reason,resource_key,library_state,is_manual_only,eligibility,exclusion_reason,profile_id,detail_url,metadata_json,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (candidate_id, task_id, item["id"], index, title, first_value(torrent, ("site_name", "site")),
                             first_value(torrent, ("size", "size_bytes")), first_value(torrent, ("seeders", "seeder")), analyzed["resolution"],
                             analyzed["codec"], analyzed["group"], analyzed["tier"], analyzed["score"], json_value(analyzed["breakdown"]),
                             analyzed["ranking"], analyzed["recommendation"], analyzed["reason"], fingerprint, state,
                             int(analyzed["manual"]), "eligible" if analyzed["eligible"] else "excluded",
                             analyzed.get("exclusion_reason"), analyzed.get("profile_id"), first_value(torrent, ("detail_url",)), json_value(metadata), utc_now()),
                        )
                    remember_raw_candidate(candidate_id, {"media": source_media, "torrent": torrent, "tmdb_id": tmdb_id})
                matched += len(eligible_keys)
                task_log(
                    task_id, "info", "candidate",
                    f"{label} 保留 {len(eligible_keys)} 个可下载候选，记录 {len(excluded_keys)} 个排除样本",
                )
            except Exception as exc:
                reason = safe_error(exc)
                warnings.append(f"{label}：{reason}")
                task_log(task_id, "error", "movie", f"{label} 处理失败：{reason}")
            update_task(task_id, completed=completed, matched=matched)
        status = "partial" if warnings else "completed"
        message = "；".join(warnings[:5])[:500] if warnings else None
        update_task(task_id, status=status, completed=len(items), matched=matched, error_message=message)
        task_log(task_id, "warning" if warnings else "info", "task", f"任务结束：{len(items)} 部已处理，{matched} 个候选，{len(warnings)} 个警告")
    except asyncio.CancelledError:
        update_task(task_id, status="cancelled")
        raise
    except Exception as exc:
        reason = safe_error(exc)
        update_task(task_id, status="failed", error_message=reason)
        task_log(task_id, "error", "task", f"任务异常停止：{reason}")
    finally:
        running_tasks.pop(task_id, None)

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
        if failed_only:
            failed = conn.execute(
                """SELECT DISTINCT site_id,playlist_item_id FROM search_attempts
                   WHERE task_id=? AND status='failed' AND site_id IS NOT NULL""", (task_id,),
            ).fetchall()
            site_ids = sorted({int(row["site_id"]) for row in failed})
            item_ids = sorted({int(row["playlist_item_id"]) for row in failed})
            pair_scope = sorted([
                [int(row["playlist_item_id"]), int(row["site_id"])]
                for row in failed
            ])
            if not failed:
                raise HTTPException(422, "该任务没有可重试的站点失败记录")
        else:
            try:
                site_ids = [int(value) for value in json.loads(source["site_ids_json"] or "[]")]
                item_ids = [int(value) for value in json.loads(source["item_ids_json"] or "[]")]
                pair_scope = json.loads(source["pair_scope_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                site_ids, item_ids, pair_scope = [], [], []
        total = len(item_ids) if item_ids else int(source["total"])
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
    return int(new_id), total
