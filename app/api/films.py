"""Film-centred routes for the “电影藏馆” interface.

Film status, the home page, the pick queue, the timeline and poster proxies;
searching and submission reuse the search and cart services.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from ..clients import FANART_POSTER_URL, EmbyClient, FanartClient, TMDBClient
from ..config import settings
from ..database import connect, json_value
from ..domain.titles import canonical_item_title, canonical_item_year
from ..logs import event_logger
from ..schemas import FilmTmdbPayload
from ..security import safe_error, sanitize_sensitive_text
from .logs import log_events
from ..services.candidates import present_candidates
from ..services.films import (
    FILM_ISSUE_LABELS,
    backdrop_url,
    current_candidate_tasks,
    fanart_version,
    FILM_STATUSES,
    matches_query,
    project_films,
    reidentify_item,
    status_counts,
    store_library_state,
)
from ..services.library import hydrate_recent_emby_posters, library_details
from ..services.recognition import TMDB_POSTER_PATH, recognize_item
from ..services.search import begin_search_task_slot, run_search
from ..state import poster_cache, remember_poster, running_tasks
from ..util import raster_image_media_type, rows_to_dicts, to_int, utc_now

router = APIRouter()
MAX_FILM_PAGE_SIZE = 200
# 首页海报架最多返回的影片数；界面按可用宽度只显示一整行。
HOME_SHELF_SIZE = 16
FILM_ITEM_COLUMNS = """i.id,i.playlist_id,i.rank_no,i.imdb_id,i.original_title,i.year,i.chinese_title,
    i.tmdb_id,i.tmdb_title,i.tmdb_original_title,i.tmdb_year,i.tmdb_imdb_id,i.tmdb_poster_path,i.fanart_poster_url,
    i.source_tmdb_id,i.source_ref,i.fanart_backdrop_url,i.tmdb_backdrop_path,
    i.emby_item_id,i.emby_image_tag,i.library_state,i.library_checked_at"""


def _playlists(conn: Any) -> list[dict[str, Any]]:
    return rows_to_dicts(conn.execute(
        """SELECT p.id,p.name,p.source_type,COUNT(i.id) AS item_count FROM playlists p
           LEFT JOIN playlist_items i ON i.playlist_id=p.id GROUP BY p.id ORDER BY p.position,p.id"""
    ).fetchall())


def _playlist_items(conn: Any, playlist_id: int | None) -> list[dict[str, Any]]:
    if playlist_id is None:
        rows = conn.execute(
            f"""SELECT {FILM_ITEM_COLUMNS} FROM playlist_items i JOIN playlists p ON p.id=i.playlist_id
                ORDER BY p.position,p.id,i.rank_no"""  # nosec B608
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {FILM_ITEM_COLUMNS} FROM playlist_items i WHERE i.playlist_id=? ORDER BY i.rank_no",  # nosec B608
            (playlist_id,),
        ).fetchall()
    return rows_to_dicts(rows)


def _valid_status_filter(status: str) -> str:
    if status == "all" or status in FILM_STATUSES:
        return status
    if status.startswith("issue:") and status.split(":", 1)[1] in FILM_ISSUE_LABELS:
        return status
    raise HTTPException(422, "不支持的状态筛选")


def _status_matches(film: dict[str, Any], status: str) -> bool:
    if status == "all":
        return True
    if status.startswith("issue:"):
        return status.split(":", 1)[1] in film["issues"]
    return film["status"] == status


@router.get("/api/films")
async def films(
    playlist_id: int | None = None,
    status: str = "all",
    q: str = Query(default="", max_length=120),
    page: int = 1,
    page_size: int = 60,
) -> dict[str, Any]:
    status = _valid_status_filter(status)
    safe_page_size = max(1, min(to_int(page_size, 60), MAX_FILM_PAGE_SIZE))
    with connect() as conn:
        playlists = _playlists(conn)
        if playlist_id is not None and not any(item["id"] == playlist_id for item in playlists):
            raise HTTPException(404, "片单不存在")
        items = _playlist_items(conn, playlist_id)
    projected = await project_films(items)
    searched = [film for film in projected if matches_query(film, q)]
    counts = status_counts(searched)
    filtered = [film for film in searched if _status_matches(film, status)]
    pages = max(1, (len(filtered) + safe_page_size - 1) // safe_page_size)
    safe_page = max(1, min(to_int(page, 1), pages))
    start = (safe_page - 1) * safe_page_size
    return {
        "items": filtered[start:start + safe_page_size],
        "total": len(filtered),
        "page": safe_page,
        "page_size": safe_page_size,
        "pages": pages,
        "counts": counts,
        "playlists": playlists,
    }


def _search_summary(conn: Any, item_id: int) -> dict[str, Any] | None:
    latest = conn.execute(
        "SELECT MAX(task_id) AS task_id FROM search_attempts WHERE playlist_item_id=?", (item_id,),
    ).fetchone()
    if not latest or latest["task_id"] is None:
        return None
    summary = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                  SUM(result_count) AS results, MAX(finished_at) AS finished_at
           FROM search_attempts WHERE playlist_item_id=? AND task_id=?""",
        (item_id, latest["task_id"]),
    ).fetchone()
    return {
        "task_id": to_int(latest["task_id"]),
        "sites": to_int(summary["total"] or 0),
        "succeeded": to_int(summary["succeeded"] or 0),
        "failed": to_int(summary["failed"] or 0),
        "results": to_int(summary["results"] or 0),
        "finished_at": summary["finished_at"],
    }


def _latest_candidate_rows(conn: Any, item_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Candidates from each film's current candidate tasks (see ``current_candidate_tasks``), batched."""
    if not item_ids:
        return {}
    chains = current_candidate_tasks(conn, item_ids)
    if not chains:
        return {}
    marks = ",".join("?" for _ in chains)
    rows = conn.execute(
        f"""SELECT c.*, p.rank_no, p.original_title, p.year, p.chinese_title,
                   p.tmdb_title,p.tmdb_original_title,p.tmdb_year,p.tmdb_imdb_id,
                   CASE WHEN cart.candidate_id IS NULL THEN 0 ELSE 1 END AS in_cart
            FROM candidates c
            JOIN playlist_items p ON p.id=c.playlist_item_id
            LEFT JOIN cart_items cart ON cart.candidate_id=c.id
            WHERE c.playlist_item_id IN ({marks})
            ORDER BY c.playlist_item_id, c.ranking""",  # nosec B608
        list(chains),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows_to_dicts(rows):
        item_id = to_int(row["playlist_item_id"])
        if to_int(row["task_id"]) in chains[item_id]:
            grouped.setdefault(item_id, []).append(row)
    return grouped


def _candidate_view(rows: list[dict[str, Any]], site_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Split one film's candidates into selectable ones and a summary of exclusion reasons."""
    grouped = present_candidates(rows, site_rows)
    excluded: dict[str, int] = {}
    for candidate in grouped:
        if candidate.get("eligibility") == "excluded":
            reason = str(candidate.get("exclusion_reason") or "其他原因")
            excluded[reason] = excluded.get(reason, 0) + 1
    return {
        "candidates": [candidate for candidate in grouped if candidate.get("eligibility") != "excluded"],
        "excluded_summary": [
            {"reason": reason, "count": count} for reason, count in sorted(excluded.items(), key=lambda pair: -pair[1])
        ],
        "excluded_count": sum(excluded.values()),
    }


@router.get("/api/films/{item_id}")
async def film_detail(item_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT {FILM_ITEM_COLUMNS}, p.name AS playlist_name FROM playlist_items i
                JOIN playlists p ON p.id=i.playlist_id WHERE i.id=?""",  # nosec B608
            (item_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "影片不存在")
        item = dict(row)
        candidate_rows = _latest_candidate_rows(conn, [item_id]).get(item_id, [])
        site_rows = rows_to_dicts(conn.execute("SELECT name,priority,icon_url FROM pt_sites").fetchall())
        history = rows_to_dicts(conn.execute(
            """SELECT h.id,h.title,h.torrent_name,h.site_name,h.success,h.message,h.created_at
               FROM download_history h LEFT JOIN candidates c ON c.id=h.candidate_id
               WHERE COALESCE(h.playlist_item_id, c.playlist_item_id)=? ORDER BY h.id DESC LIMIT 20""",
            (item_id,),
        ).fetchall())
        search = _search_summary(conn, item_id)
        search_sites = to_int(conn.execute(
            "SELECT COUNT(*) FROM pt_sites WHERE enabled=1 AND search_enabled=1"
        ).fetchone()[0])
    await hydrate_recent_emby_posters([item])
    film = (await project_films([item]))[0]
    for record in history:
        record["message"] = sanitize_sensitive_text(record.get("message") or "", 300)
    return {
        **film,
        "backdrop_url": backdrop_url(item),
        "playlist_name": item["playlist_name"],
        "library_checked_at": item.get("library_checked_at"),
        **_candidate_view(candidate_rows, site_rows),
        "history": history,
        "search": search,
        "search_site_count": search_sites,
    }


@router.post("/api/films/{item_id}/search")
async def search_film(item_id: int) -> dict[str, Any]:
    with connect() as conn:
        item = conn.execute(
            "SELECT id,playlist_id,rank_no,library_state FROM playlist_items WHERE id=?", (item_id,),
        ).fetchone()
        if not item:
            raise HTTPException(404, "影片不存在")
        if item["library_state"] == "in_library":
            raise HTTPException(409, "这部影片已经入馆")
        for task in conn.execute(
            "SELECT item_ids_json FROM search_tasks WHERE status IN ('queued','running') AND playlist_id=?",
            (item["playlist_id"],),
        ).fetchall():
            if item_id in (json_ids(task["item_ids_json"]) or []):
                raise HTTPException(409, "这部影片正在寻片")
        begin_search_task_slot(conn)
        site_ids = [
            to_int(row["id"]) for row in conn.execute(
                "SELECT id FROM pt_sites WHERE enabled=1 AND search_enabled=1 ORDER BY priority,id",
            ).fetchall()
        ]
        if not site_ids:
            raise HTTPException(422, "请先在设置中选择至少一个参与搜索的站点")
        rank = to_int(item["rank_no"] or 0)
        task_id = conn.execute(
            """INSERT INTO search_tasks(
                 playlist_id,range_start,range_end,status,total,trigger,site_ids_json,item_ids_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (item["playlist_id"], rank, rank, "queued", 1, "film", json_value(site_ids), json_value([item_id]),
             utc_now(), utc_now()),
        ).lastrowid
    if task_id is None:
        raise HTTPException(500, "寻片任务写入失败")
    running_tasks[task_id] = asyncio.create_task(run_search(task_id))
    return {"id": task_id, "status": "queued", "total": 1}


def json_ids(raw: Any) -> list[int] | None:
    try:
        values = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return [to_int(value) for value in values] if isinstance(values, list) else None


def _active_tasks(conn: Any) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for row in conn.execute(
        """SELECT t.id,t.status,t.total,t.completed,t.trigger,t.playlist_id,p.name AS playlist_name
           FROM search_tasks t JOIN playlists p ON p.id=t.playlist_id
           WHERE t.status IN ('queued','running') ORDER BY t.id"""
    ).fetchall():
        tasks.append({"kind": "search", **dict(row)})
    for table, kind in (("recognition_tasks", "recognition"), ("library_scan_tasks", "library")):
        for row in conn.execute(
            f"""SELECT t.id,t.status,t.total,t.completed,t.playlist_id,p.name AS playlist_name
                FROM {table} t JOIN playlists p ON p.id=t.playlist_id
                WHERE t.status IN ('queued','running') ORDER BY t.id"""  # nosec B608
        ).fetchall():
            tasks.append({"kind": kind, **dict(row)})
    for row in conn.execute(
        """SELECT r.id,r.status,r.total,r.completed,r.stage,r.playlist_id,p.name AS playlist_name
           FROM automation_runs r JOIN playlists p ON p.id=r.playlist_id
           WHERE r.status IN ('queued','running') ORDER BY r.id"""
    ).fetchall():
        tasks.append({"kind": "automation", **dict(row)})
    return tasks


@router.get("/api/home")
async def home(playlist_id: int | None = None) -> dict[str, Any]:
    with connect() as conn:
        playlists = _playlists(conn)
        if playlist_id is None and playlists:
            playlist_id = to_int(playlists[0]["id"])
        if playlist_id is not None and not any(item["id"] == playlist_id for item in playlists):
            raise HTTPException(404, "片单不存在")
        items = _playlist_items(conn, playlist_id) if playlist_id is not None else []
        failing_sites = [
            str(row["name"]) for row in conn.execute(
                "SELECT name FROM pt_sites WHERE enabled=1 AND search_enabled=1 AND last_status='error' ORDER BY priority,id"
            ).fetchall()
        ]
        empty_sites = [
            str(row["name"]) for row in conn.execute(
                "SELECT name FROM pt_sites WHERE enabled=1 AND search_enabled=1 AND last_status='empty' ORDER BY priority,id"
            ).fetchall()
        ]
        tasks = _active_tasks(conn)
    projected = await project_films(items)
    counts = status_counts(projected)
    in_library = [item for item in items if item.get("library_state") == "in_library"]
    if settings.dashboard_random_posters:
        # “首页随机海报”：每天换一组，同一天内保持稳定。
        seed = to_int(datetime.now(timezone.utc).strftime("%Y%m%d"))
        recent_items = sorted(in_library, key=lambda item: (to_int(item["id"]) * 1103515245 + seed) & 0x7FFFFFFF)[:HOME_SHELF_SIZE]
    else:
        recent_items = sorted(in_library, key=lambda item: str(item.get("library_checked_at") or ""), reverse=True)[:HOME_SHELF_SIZE]
    await hydrate_recent_emby_posters(recent_items)
    recent_ids = [to_int(item["id"]) for item in recent_items]
    by_id = {film["id"]: film for film in await project_films(recent_items)} if recent_items else {}
    todos: list[dict[str, Any]] = []

    def add(key: str, count: int, **extra: Any) -> None:
        if count:
            todos.append({"key": key, "count": count, **extra})

    searchable = [film for film in projected if film["status"] == "missing" and "no_eligible" not in film["issues"]]
    add("search_missing", len(searchable))
    add("pick", counts["candidates"])
    add("submit", counts["selected"])
    add("unrecognized", counts["unrecognized"])
    add("unchecked", counts["unchecked"], emby_configured=bool(settings.emby_base_url and settings.emby_api_key))
    add("no_eligible", counts["issue:no_eligible"])
    add("submit_failed", counts["issue:submit_failed"])
    add("context_expired", counts["issue:context_expired"])
    add("failing_sites", len(failing_sites), names=failing_sites[:5])
    add("empty_sites", len(empty_sites), names=empty_sites[:5])
    return {
        "playlists": playlists,
        "playlist_id": playlist_id,
        "counts": counts,
        "todos": todos,
        "tasks": tasks,
        "recent": [by_id[item_id] for item_id in recent_ids if item_id in by_id],
        # 下一批寻片会按片单顺序处理的缺片（与“为缺片寻片”按钮的范围一致）。
        "up_next": searchable[:HOME_SHELF_SIZE],
    }


@router.get("/api/playlist-items/{item_id}/tmdb-poster")
async def playlist_item_tmdb_poster(item_id: int) -> Response:
    """Proxy a TMDB poster so films outside Emby still get artwork without exposing the TMDB key."""
    with connect() as conn:
        row = conn.execute("SELECT tmdb_id,tmdb_poster_path FROM playlist_items WHERE id=?", (item_id,)).fetchone()
    if not row or not row["tmdb_id"]:
        raise HTTPException(404, "影片尚未识别")
    poster_path = row["tmdb_poster_path"]
    if poster_path is None:
        if not settings.tmdb_api_key:
            raise HTTPException(404, "未配置 TMDB，无法读取海报")
        # 1.45 之前识别的影片没有保存海报路径：首次访问时向 TMDB 补取一次并记住结果。
        try:
            details = await TMDBClient().movie_details(to_int(row["tmdb_id"]))
        except Exception as exc:
            raise HTTPException(502, f"TMDB 海报信息读取失败：{safe_error(exc)}") from exc
        candidate = str(details.get("poster_path") or "")
        poster_path = candidate if TMDB_POSTER_PATH.fullmatch(candidate) else ""
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET tmdb_poster_path=? WHERE id=?", (poster_path, item_id))
    if not poster_path or not TMDB_POSTER_PATH.fullmatch(poster_path):
        raise HTTPException(404, "TMDB 没有这部影片的海报")
    cache_key = f"tmdb:{poster_path}"
    if cache_key in poster_cache:
        content, media_type = poster_cache[cache_key]
    else:
        try:
            content, _ = await TMDBClient().poster_image(poster_path)
        except httpx.HTTPStatusError as exc:
            status = 404 if exc.response.status_code == 404 else 502
            raise HTTPException(status, "TMDB 海报读取失败") from exc
        except Exception as exc:
            raise HTTPException(502, f"TMDB 海报读取失败：{safe_error(exc)}") from exc
        media_type = raster_image_media_type(content) or ""
        if not media_type:
            raise HTTPException(422, "TMDB 返回的海报格式无效")
        remember_poster(cache_key, (content, media_type))
    return Response(
        content=content, media_type=media_type,
        headers={"Cache-Control": "private, max-age=604800", "X-Content-Type-Options": "nosniff"},
    )




FALLBACK_POSTER_CACHE = "private, max-age=3600"


async def _backfill_original_language(item_id: int, tmdb_id: int) -> str | None:
    """1.50 之前识别的影片没有保存原语言：挑 fanart 海报前向 TMDB 补取一次。"""
    if not settings.tmdb_api_key:
        return None
    from ..services.recognition import tmdb_original_language

    language = tmdb_original_language(await TMDBClient().movie_details(tmdb_id))
    if language:
        with connect() as conn:
            conn.execute(
                "UPDATE playlist_items SET tmdb_original_language=? WHERE id=? AND tmdb_id=?", (language, item_id, tmdb_id),
            )
    return language


@router.get("/api/playlist-items/{item_id}/backdrop")
async def playlist_item_backdrop(item_id: int, v: str = "") -> Response:
    """影片详情横幅：fanart.tv 剧照优先，没有时用 TMDB 剧照（w1280）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT tmdb_id,fanart_backdrop_url,tmdb_backdrop_path FROM playlist_items WHERE id=?", (item_id,),
        ).fetchone()
    if not row or not row["tmdb_id"]:
        raise HTTPException(404, "影片尚未识别")
    tmdb_id = to_int(row["tmdb_id"])
    fanart_url, tmdb_path = row["fanart_backdrop_url"], row["tmdb_backdrop_path"]

    def remember(column: str, value: str) -> None:
        with connect() as conn:
            # 列名只来自下面两个固定值。
            conn.execute(f"UPDATE playlist_items SET {column}=? WHERE id=? AND tmdb_id=?", (value, item_id, tmdb_id))  # nosec B608

    if fanart_url is None and settings.fanart_api_key:
        try:
            fanart_url = await FanartClient().movie_background_url(tmdb_id)
        except Exception as exc:
            event_logger().warning("fanart_backdrop_lookup_failed", extra={"detail": f"#{item_id} {safe_error(exc)}"})
        else:
            remember("fanart_backdrop_url", fanart_url)
    image: tuple[bytes, str] | None = None
    chosen = ""
    if fanart_url and FANART_POSTER_URL.fullmatch(fanart_url):
        chosen = fanart_url
        image = poster_cache.get(f"fanart-bg:{fanart_url}")
        if image is None:
            try:
                content, _ = await FanartClient().background_image(fanart_url)
                media_type = raster_image_media_type(content) or ""
                if media_type:
                    image = (content, media_type)
                    remember_poster(f"fanart-bg:{fanart_url}", image)
            except Exception as exc:
                event_logger().warning("fanart_backdrop_fetch_failed", extra={"detail": f"#{item_id} {safe_error(exc)}"})
    if image is None and settings.tmdb_api_key:
        if tmdb_path is None:
            try:
                candidate = str((await TMDBClient().movie_details(tmdb_id)).get("backdrop_path") or "")
            except Exception as exc:
                raise HTTPException(502, f"TMDB 剧照信息读取失败：{safe_error(exc)}") from exc
            tmdb_path = candidate if TMDB_POSTER_PATH.fullmatch(candidate) else ""
            remember("tmdb_backdrop_path", tmdb_path)
        if tmdb_path and TMDB_POSTER_PATH.fullmatch(tmdb_path):
            chosen = chosen or tmdb_path
            image = poster_cache.get(f"tmdb-bg:{tmdb_path}")
            if image is None:
                try:
                    content, _ = await TMDBClient().poster_image(tmdb_path, size="w1280")
                except Exception as exc:
                    raise HTTPException(502, f"TMDB 剧照读取失败：{safe_error(exc)}") from exc
                media_type = raster_image_media_type(content) or ""
                if not media_type:
                    raise HTTPException(422, "TMDB 返回的剧照格式无效")
                image = (content, media_type)
                remember_poster(f"tmdb-bg:{tmdb_path}", image)
    if image is None:
        raise HTTPException(404, "没有可用的剧照")
    cache = "private, max-age=604800" if v == fanart_version(chosen) else FALLBACK_POSTER_CACHE
    return Response(content=image[0], media_type=image[1], headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff"})


@router.get("/api/playlist-items/{item_id}/fanart-poster")
async def playlist_item_fanart_poster(item_id: int, v: str = "") -> Response:
    """Proxy a fanart.tv poster; when fanart.tv has none, serve the Emby/TMDB poster instead.

    只有地址里的版本号与当前海报一致时才允许浏览器长期缓存；“待定”地址返回的是本次刚选出的海报，
    之后列表会换成带版本号的地址，因此只短期缓存，重新挑选海报后不会继续显示旧图。
    """
    with connect() as conn:
        row = conn.execute(
            """SELECT tmdb_id,tmdb_original_language,fanart_poster_url,emby_item_id,emby_image_tag
               FROM playlist_items WHERE id=?""", (item_id,),
        ).fetchone()
    if not row or not row["tmdb_id"]:
        raise HTTPException(404, "影片尚未识别")
    poster_url = row["fanart_poster_url"]
    if poster_url is None and settings.fanart_api_key:
        try:
            language = row["tmdb_original_language"] or await _backfill_original_language(item_id, to_int(row["tmdb_id"]))
            poster_url = await FanartClient().movie_poster_url(to_int(row["tmdb_id"]), language)
        except Exception as exc:
            # 网络或密钥问题不记为“没有海报”，下次仍会重试 fanart.tv。
            event_logger().warning("fanart_poster_lookup_failed", extra={"detail": f"#{item_id} {safe_error(exc)}"})
            poster_url = None
        else:
            with connect() as conn:
                conn.execute(
                    "UPDATE playlist_items SET fanart_poster_url=? WHERE id=? AND tmdb_id=?",
                    (poster_url, item_id, row["tmdb_id"]),
                )
    if poster_url and FANART_POSTER_URL.fullmatch(poster_url):
        cache_key = f"fanart:{poster_url}"
        cached = poster_cache.get(cache_key)
        if cached is None:
            try:
                content, _ = await FanartClient().poster_image(poster_url)
            except Exception as exc:
                event_logger().warning("fanart_poster_fetch_failed", extra={"detail": f"#{item_id} {safe_error(exc)}"})
            else:
                media_type = raster_image_media_type(content) or ""
                if media_type:
                    cached = (content, media_type)
                    remember_poster(cache_key, cached)
        if cached is not None:
            cache = "private, max-age=604800" if v == fanart_version(poster_url) else FALLBACK_POSTER_CACHE
            return Response(
                content=cached[0], media_type=cached[1],
                headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff"},
            )
    # 回退到 Emby / TMDB 海报；缓存时间较短，fanart.tv 之后补上海报时能及时换成新图。
    from .playlists import playlist_item_poster

    if row["emby_item_id"]:
        try:
            response = await playlist_item_poster(item_id, str(row["emby_image_tag"] or ""))
        except HTTPException:
            response = None
        if response is not None:
            response.headers["Cache-Control"] = FALLBACK_POSTER_CACHE
            return response
    if not settings.tmdb_api_key:
        raise HTTPException(404, "没有可用的海报")
    response = await playlist_item_tmdb_poster(item_id)
    response.headers["Cache-Control"] = FALLBACK_POSTER_CACHE
    return response

PICK_BUCKETS = ("candidates", "selected", "no_eligible")


def _pick_bucket(film: dict[str, Any]) -> str | None:
    if film["status"] in {"candidates", "selected"}:
        return film["status"]
    if film["status"] == "missing" and "no_eligible" in film["issues"]:
        return "no_eligible"
    return None


@router.get("/api/picks")
async def picks(playlist_id: int | None = None, status: str = "all") -> dict[str, Any]:
    """挑选台：有候选、已选定与无合格资源的影片，按片单顺序，附带候选行。"""
    if status != "all" and status not in PICK_BUCKETS:
        raise HTTPException(422, "不支持的挑选筛选")
    with connect() as conn:
        playlists = _playlists(conn)
        if playlist_id is not None and not any(item["id"] == playlist_id for item in playlists):
            raise HTTPException(404, "片单不存在")
        items = _playlist_items(conn, playlist_id)
    projected = await project_films(items)
    pickable = [film for film in projected if _pick_bucket(film)]
    counts = {"all": len(pickable), **{bucket: sum(1 for film in pickable if _pick_bucket(film) == bucket) for bucket in PICK_BUCKETS}}
    chosen = [film for film in pickable if status == "all" or _pick_bucket(film) == status]
    with connect() as conn:
        rows_by_item = _latest_candidate_rows(conn, [film["id"] for film in chosen])
        site_rows = rows_to_dicts(conn.execute("SELECT name,priority,icon_url FROM pt_sites").fetchall())
    return {
        "items": [
            {**film, "bucket": _pick_bucket(film), **_candidate_view(rows_by_item.get(film["id"], []), site_rows)}
            for film in chosen
        ],
        "counts": counts,
        "playlists": playlists,
    }


def _film_row(item_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(f"SELECT {FILM_ITEM_COLUMNS} FROM playlist_items i WHERE i.id=?", (item_id,)).fetchone()  # nosec B608
    if not row:
        raise HTTPException(404, "影片不存在")
    return dict(row)


async def _recheck_library(item_id: int) -> None:
    """身份变更后立即重新核对这一部的 Emby 状态；未配置 Emby 时保持“待核对”。"""
    if not settings.emby_base_url or not settings.emby_api_key:
        return
    item = _film_row(item_id)
    state, emby_item_id, image_tag = await library_details(
        EmbyClient(), canonical_item_title(item), canonical_item_year(item),
        item.get("tmdb_id"), item.get("tmdb_imdb_id") or item.get("imdb_id"),
    )
    store_library_state(item_id, state, emby_item_id, image_tag)


@router.post("/api/films/{item_id}/recognize")
async def recognize_film(item_id: int) -> dict[str, Any]:
    """按导入时的原名、年份与 IMDb 重新识别这一部影片（整体替换原有识别结果）。"""
    item = _film_row(item_id)
    if not settings.tmdb_api_key:
        raise HTTPException(422, "请先在设置中填写 TMDB API Key")
    try:
        media = await recognize_item(item)
    except Exception as exc:
        raise HTTPException(502, f"TMDB 识别失败：{safe_error(exc)}") from exc
    if not media:
        raise HTTPException(422, "TMDB 没有找到可靠的匹配结果，可以在下方手动指定")
    reidentify_item(item_id, media, item.get("imdb_id"))
    await _recheck_library(item_id)
    event_logger().info(
        "film_reidentified",
        extra={"movie": item["original_title"], "detail": f"重新识别为 TMDB {media.get('id')}（{media.get('title') or media.get('original_title')}）"},
    )
    return await film_detail(item_id)


@router.get("/api/films/{item_id}/tmdb-matches")
async def tmdb_matches(item_id: int, q: str = Query(default="", max_length=120), year: int | None = None) -> list[dict[str, Any]]:
    """手动指定用：按关键词在 TMDB 搜索候选影片。"""
    item = _film_row(item_id)
    if not settings.tmdb_api_key:
        raise HTTPException(422, "请先在设置中填写 TMDB API Key")
    query = q.strip() or str(item["original_title"])
    search_year = year if q.strip() else (year or item.get("year"))
    try:
        options = await TMDBClient().search_movie(query, search_year)
        if not options and search_year:
            options = await TMDBClient().search_movie(query, None)
    except Exception as exc:
        raise HTTPException(502, f"TMDB 搜索失败：{safe_error(exc)}") from exc
    results = []
    for option in options[:10]:
        release = str(option.get("release_date") or "")
        results.append({
            "tmdb_id": to_int(option.get("id")),
            "title": option.get("title") or option.get("original_title") or "",
            "original_title": option.get("original_title") or "",
            "year": to_int(release[:4]) if release[:4].isdigit() else None,
            "overview": str(option.get("overview") or "")[:140],
            "current": to_int(option.get("id")) == to_int(item.get("tmdb_id")),
        })
    return results


@router.post("/api/films/{item_id}/tmdb")
async def set_film_tmdb(item_id: int, payload: FilmTmdbPayload) -> dict[str, Any]:
    """手动指定 TMDB 影片，覆盖自动识别结果。"""
    item = _film_row(item_id)
    if not settings.tmdb_api_key:
        raise HTTPException(422, "请先在设置中填写 TMDB API Key")
    try:
        details = await TMDBClient().movie_details(payload.tmdb_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, "TMDB 上不存在这个影片编号") from exc
        raise HTTPException(502, f"TMDB 读取失败：{safe_error(exc)}") from exc
    except Exception as exc:
        raise HTTPException(502, f"TMDB 读取失败：{safe_error(exc)}") from exc
    if to_int(details.get("id")) != payload.tmdb_id:
        raise HTTPException(502, "TMDB 返回的影片编号不一致")
    reidentify_item(item_id, details, item.get("imdb_id"))
    await _recheck_library(item_id)
    event_logger().info(
        "film_reidentified",
        extra={"movie": item["original_title"], "detail": f"手动指定为 TMDB {payload.tmdb_id}（{details.get('title') or details.get('original_title')}）"},
    )
    return await film_detail(item_id)


# ---------- 动态时间线 ----------

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
    for row in conn.execute(
        """SELECT h.id,h.created_at,h.success,h.message,h.site_name,h.torrent_name,
                  COALESCE(h.playlist_item_id,c.playlist_item_id) AS film_id,
                  p.tmdb_title,p.chinese_title,p.original_title,p.library_state
           FROM download_history h LEFT JOIN candidates c ON c.id=h.candidate_id
           LEFT JOIN playlist_items p ON p.id=COALESCE(h.playlist_item_id,c.playlist_item_id)
           ORDER BY h.id DESC LIMIT ?""",
        (limit,),
    ).fetchall():
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
    for row in conn.execute(
        """SELECT t.id,t.status,t.total,t.completed,t.matched,t.trigger,t.item_ids_json,t.error_message,
                  t.created_at,t.updated_at,p.name AS playlist_name
           FROM search_tasks t JOIN playlists p ON p.id=t.playlist_id ORDER BY t.id DESC LIMIT ?""",
        (limit,),
    ).fetchall():
        item_ids = json_ids(row["item_ids_json"]) or []
        film_id = item_ids[0] if len(item_ids) == 1 else None
        if film_id is not None:
            film = conn.execute("SELECT tmdb_title,chinese_title,original_title FROM playlist_items WHERE id=?", (film_id,)).fetchone()
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
    for table, kind, label in (
        ("recognition_tasks", "recognition", "TMDB 识别"),
        ("library_scan_tasks", "library", "刷新 Emby 状态"),
    ):
        amount = "t.matched" if kind == "recognition" else "t.in_library"
        mode = "t.mode,t.corrected" if kind == "recognition" else "'missing' AS mode,0 AS corrected"
        for row in conn.execute(
            f"""SELECT t.id,t.status,t.total,t.completed,{amount} AS amount,{mode},
                       t.error_message,t.created_at,t.updated_at,p.name AS playlist_name
                FROM {table} t JOIN playlists p ON p.id=t.playlist_id ORDER BY t.id DESC LIMIT ?""",  # nosec B608
            (limit,),
        ).fetchall():
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
    for row in conn.execute(
        "SELECT id,level,title,message,created_at FROM notifications ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall():
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


@router.get("/api/timeline")
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
