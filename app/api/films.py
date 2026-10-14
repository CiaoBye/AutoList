"""影片：列表、详情、单片寻片与识别修正（重新识别、手动指定 TMDB）。"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

from ..clients import EmbyClient, TMDBClient
from ..config import settings
from ..database import connect
from ..domain.titles import canonical_item_title, canonical_item_year
from ..logs import event_logger
from ..queries.films import film_history, film_item, playlist_items, playlist_summaries, search_summary
from ..queries.search import insert_search_task, item_in_active_search
from ..queries.sites import searchable_site_ids, site_display_rows
from ..schemas import FilmsUpgradePayload, FilmTmdbPayload
from ..security import safe_error, sanitize_sensitive_text
from ..services.candidates import candidate_view, latest_candidate_rows
from ..services.films import (
    clear_candidates,
    dismiss_submit_failure,
    FILM_ISSUE_LABELS,
    FILM_STATUSES,
    backdrop_url,
    matches_query,
    project_films,
    reidentify_item,
    status_counts,
    store_library_state,
)
from ..services.library import hydrate_recent_emby_posters, library_details
from ..services.recognition import localize_media, recognize_item
from ..services.search import begin_search_task_slot, run_search
from ..tasks import SEARCH
from ..util import to_int
from ..services.identity import identity_flag
from ..responses import FilmCleared, FilmDetail, FilmPage, FilmSearchStarted, FilmsUpgradeStarted, TmdbMatch

router = APIRouter()
MAX_FILM_PAGE_SIZE = 200


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


@router.get("/api/films", response_model=FilmPage)
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
        playlists = playlist_summaries(conn)
        if playlist_id is not None and not any(item["id"] == playlist_id for item in playlists):
            raise HTTPException(404, "片单不存在")
        items = playlist_items(conn, playlist_id)
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


async def _library_versions(item: dict[str, Any]) -> list[dict[str, Any]]:
    """已入馆影片在 Emby 里的全部实体文件版本；Emby 查询失败不影响详情。"""
    if not settings.emby_base_url or not settings.emby_api_key:
        return []
    try:
        return await EmbyClient().versions(item.get("tmdb_id"), item.get("tmdb_imdb_id") or item.get("imdb_id"))
    except Exception:
        return []


def _best_tier(versions: list[dict[str, Any]]) -> int | None:
    tiers = [version["tier"] for version in versions if version.get("tier")]
    return max(tiers) if tiers else None


@router.get("/api/films/{item_id}", response_model=FilmDetail)
async def film_detail(item_id: int) -> dict[str, Any]:
    with connect() as conn:
        item = film_item(conn, item_id, with_playlist=True)
        if not item:
            raise HTTPException(404, "影片不存在")
        candidate_rows = latest_candidate_rows(conn, [item_id]).get(item_id, [])
        site_rows = site_display_rows(conn)
        history = film_history(conn, item_id)
        search = search_summary(conn, item_id)
        search_sites = len(searchable_site_ids(conn))
        searching_now = item_in_active_search(conn, item["playlist_id"], item_id)
    await hydrate_recent_emby_posters([item])
    film = (await project_films([item]))[0]
    versions = await _library_versions(item) if film["library_state"] == "in_library" else []
    if versions and _best_tier(versions) != item.get("library_tier"):
        with connect() as conn:  # 顺带记下最新的现有画质，书目里的“可升级”据此判断
            conn.execute("UPDATE playlist_items SET library_tier=? WHERE id=?", (_best_tier(versions), item_id))
        film["library_tier"] = _best_tier(versions)
    library_versions = versions if len(versions) > 1 else []
    upgrade_above = next((row["upgrade_above"] for row in candidate_rows if row.get("upgrade_above") is not None), None)
    upgrade_searching = film["library_state"] == "in_library" and searching_now
    for record in history:
        record["message"] = sanitize_sensitive_text(record.get("message") or "", 300)
    return {
        **film,
        "backdrop_url": backdrop_url(item),
        "playlist_name": item["playlist_name"],
        "library_checked_at": item.get("library_checked_at"),
        **candidate_view(candidate_rows, site_rows),
        "next_search_at": item.get("next_search_at"),
        "identity_flag": identity_flag(item) if "no_eligible" in film["issues"] else None,
        "library_versions": library_versions,
        "library_tier": _best_tier(versions),
        "upgrade_above": upgrade_above,
        "upgrade_searching": upgrade_searching,
        "upgrade_enabled": settings.upgrade_enabled,
        "history": history,
        "search": search,
        "search_site_count": search_sites,
    }


@router.post("/api/films/{item_id}/clear-candidates", response_model=FilmCleared)
async def clear_film_candidates(item_id: int) -> dict[str, Any]:
    """清除这部影片没选定的候选，让它回到“缺片”、重新进入批量寻片；已选定的保留。"""
    with connect() as conn:
        if not film_item(conn, item_id):
            raise HTTPException(404, "影片不存在")
        if item_in_active_search(conn, film_item(conn, item_id)["playlist_id"], item_id):
            raise HTTPException(409, "这部影片正在寻片")
    return {"cleared": clear_candidates(item_id)}


@router.post("/api/films/{item_id}/dismiss-failure", response_model=FilmCleared)
async def dismiss_film_failure(item_id: int) -> dict[str, Any]:
    """忽略这部影片的“提交失败”标记（提交记录保留）。"""
    with connect() as conn:
        if not film_item(conn, item_id):
            raise HTTPException(404, "影片不存在")
    dismiss_submit_failure(item_id)
    return {"cleared": 1}


@router.post("/api/films/{item_id}/search", response_model=FilmSearchStarted)
async def search_film(item_id: int) -> dict[str, Any]:
    with connect() as conn:
        item = film_item(conn, item_id)
        if not item:
            raise HTTPException(404, "影片不存在")
        if item["library_state"] == "in_library":
            raise HTTPException(409, "这部影片已经入馆")
        if item_in_active_search(conn, item["playlist_id"], item_id):
            raise HTTPException(409, "这部影片正在寻片")
        begin_search_task_slot(conn)
        site_ids = searchable_site_ids(conn)
        if not site_ids:
            raise HTTPException(422, "请先在馆务中选择至少一个参与搜索的站点")
        rank = to_int(item["rank_no"] or 0)
        task_id = insert_search_task(
            conn, item["playlist_id"], range_start=rank, range_end=rank, trigger="film", site_ids=site_ids, item_ids=[item_id],
        )
    SEARCH.start(task_id, run_search(task_id))
    return {"id": task_id, "status": "queued", "total": 1}


@router.post("/api/films/{item_id}/upgrade-search", response_model=FilmSearchStarted)
async def upgrade_search_film(item_id: int) -> dict[str, Any]:
    """升级搜索（洗版）：影片已入馆时找画质更高的资源；只保留高于 Emby 里现有最高画质的候选，选定后照常经 MoviePilot 提交。"""
    _require_upgrade_enabled()
    with connect() as conn:
        item = film_item(conn, item_id)
    if not item:
        raise HTTPException(404, "影片不存在")
    if item["library_state"] != "in_library":
        raise HTTPException(409, "只有已入馆的影片才能升级")
    if not settings.emby_base_url or not settings.emby_api_key:
        raise HTTPException(422, "未配置 Emby，无法知道现有画质")
    tier = _best_tier(await _library_versions(item))
    if tier is not None:
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET library_tier=? WHERE id=?", (tier, item_id))
    if tier is None:
        raise HTTPException(409, "Emby 里没有读到这部影片的画质，无法判断是否需要升级")
    if tier >= 2160:
        raise HTTPException(409, "已经是 2160p，没有更高的画质可升级")
    with connect() as conn:
        if item_in_active_search(conn, item["playlist_id"], item_id):
            raise HTTPException(409, "这部影片正在寻片")
        begin_search_task_slot(conn)
        site_ids = searchable_site_ids(conn)
        if not site_ids:
            raise HTTPException(422, "请先在馆务中选择至少一个参与搜索的站点")
        rank = to_int(item["rank_no"] or 0)
        task_id = insert_search_task(
            conn, item["playlist_id"], range_start=rank, range_end=rank, trigger="upgrade", site_ids=site_ids,
            item_ids=[item_id], upgrade_above=tier,
        )
    event_logger().info("film_upgrade_search", extra={"movie": item["original_title"], "detail": f"升级搜索：现有最高 {tier}p"})
    SEARCH.start(task_id, run_search(task_id))
    return {"id": task_id, "status": "queued", "total": 1}


def _require_upgrade_enabled() -> None:
    if not settings.upgrade_enabled:
        raise HTTPException(409, "升级画质功能已关闭（馆务 → 馆规里可以打开）")


@router.post("/api/films/upgrade-search", response_model=FilmsUpgradeStarted)
async def upgrade_search_films(payload: FilmsUpgradePayload) -> dict[str, Any]:
    """批量升级搜索（一次最多 20 部）：按片单与现有画质分组各开一个寻片任务，站点请求照常经各站点的限速。"""
    _require_upgrade_enabled()
    skipped: list[dict[str, Any]] = []
    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    with connect() as conn:
        for item_id in dict.fromkeys(payload.ids):
            item = film_item(conn, item_id)
            title = str(item.get("tmdb_title") or item.get("chinese_title") or item.get("original_title")) if item else str(item_id)
            tier = to_int(item.get("library_tier") or 0) if item else 0
            reason = None
            if not item:
                reason = "影片不存在"
            elif item["library_state"] != "in_library":
                reason = "还没有入馆"
            elif not tier:
                reason = "还不知道现有画质，先刷新 Emby 状态"
            elif tier >= 2160:
                reason = "已经是 2160p"
            elif item_in_active_search(conn, item["playlist_id"], item_id):
                reason = "正在寻片"
            if reason:
                skipped.append({"id": item_id, "title": title, "reason": reason})
            else:
                groups.setdefault((to_int(item["playlist_id"]), tier), []).append(item)
    if not settings.emby_base_url or not settings.emby_api_key:
        raise HTTPException(422, "未配置 Emby，无法知道现有画质")
    task_ids: list[int] = []
    started = 0
    for (playlist_id, tier), items in groups.items():
        try:
            with connect() as conn:
                begin_search_task_slot(conn)
                site_ids = searchable_site_ids(conn)
                if not site_ids:
                    raise HTTPException(422, "请先在馆务中选择至少一个参与搜索的站点")
                ranks = [to_int(item["rank_no"] or 0) for item in items]
                task_id = insert_search_task(
                    conn, playlist_id, range_start=min(ranks), range_end=max(ranks), trigger="upgrade", site_ids=site_ids,
                    item_ids=[to_int(item["id"]) for item in items], upgrade_above=tier,
                )
        except HTTPException as exc:
            if not task_ids and exc.status_code != 429:
                raise
            skipped.extend({"id": to_int(item["id"]), "title": str(item["original_title"]), "reason": str(exc.detail)} for item in items)
            continue
        SEARCH.start(task_id, run_search(task_id))
        task_ids.append(task_id)
        started += len(items)
    event_logger().info("films_upgrade_search", extra={"detail": f"批量升级搜索 {started} 部"})
    return {"started": started, "task_ids": task_ids, "skipped": skipped}


def _film_row(item_id: int) -> dict[str, Any]:
    with connect() as conn:
        item = film_item(conn, item_id)
    if not item:
        raise HTTPException(404, "影片不存在")
    return item


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


@router.post("/api/films/{item_id}/recognize", response_model=FilmDetail)
async def recognize_film(item_id: int) -> dict[str, Any]:
    """按导入时的原名、年份与 IMDb 重新识别这一部影片（整体替换原有识别结果）。"""
    item = _film_row(item_id)
    if not settings.tmdb_api_key:
        raise HTTPException(422, "请先在馆务中填写 TMDB API Key")
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


@router.get("/api/films/{item_id}/tmdb-matches", response_model=list[TmdbMatch])
async def tmdb_matches(item_id: int, q: str = Query(default="", max_length=120), year: int | None = None) -> list[dict[str, Any]]:
    """手动指定用：按关键词在 TMDB 搜索候选影片。"""
    item = _film_row(item_id)
    if not settings.tmdb_api_key:
        raise HTTPException(422, "请先在馆务中填写 TMDB API Key")
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


@router.post("/api/films/{item_id}/tmdb", response_model=FilmDetail)
async def set_film_tmdb(item_id: int, payload: FilmTmdbPayload) -> dict[str, Any]:
    """手动指定 TMDB 影片，覆盖自动识别结果。"""
    item = _film_row(item_id)
    if not settings.tmdb_api_key:
        raise HTTPException(422, "请先在馆务中填写 TMDB API Key")
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
    details = await localize_media(details)
    reidentify_item(item_id, details, item.get("imdb_id"))
    await _recheck_library(item_id)
    event_logger().info(
        "film_reidentified",
        extra={"movie": item["original_title"], "detail": f"手动指定为 TMDB {payload.tmdb_id}（{details.get('title') or details.get('original_title')}）"},
    )
    return await film_detail(item_id)
