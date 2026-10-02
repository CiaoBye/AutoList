"""待入馆清单：加入与移出候选、查看清单、统一提交到 MoviePilot。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from ..clients import EmbyClient, MoviePilotClient, TransmissionClient
from ..database import connect, json_value
from ..queries import selection as queries
from ..queries.sites import all_sites
from ..domain.titles import is_transmission_downloading, normalized_download_name, torrent_matches_item
from ..security import safe_error, sanitize_sensitive_text
from ..services.history import long_stalled, playlist_item_snapshot
from ..services.library import library_details
from ..services.cookiecloud import with_cookie_refresh
from ..services.search import wait_for_site_rate_limit
from ..sites.engine import verify_and_refresh
from ..sites.nexusphp import is_signed_download
from ..logs import event_logger
from ..state import (
    selection_submit_lock,
    forget_raw_candidate,
    prune_raw_candidates,
    raw_candidates,
)
from ..util import to_int, first_value, resource_fingerprint
from ..outbound import safe_detail_url
from ..responses import SelectionItem, SelectionToggled, SubmitResult

router = APIRouter()

# 这些适配器下载种子要带站点 Cookie；Cookie 不随候选存库，提交时按站点当前配置补上。
COOKIE_ADAPTERS = {"nexusphp", "rss"}
SITE_DELETED_REASON = "种子已被站点删除"


def _moviepilot_success(response: Any) -> bool:
    """Parse MoviePilot's success flag without treating the string 'false' as true."""
    if not isinstance(response, dict):
        return False
    value = response.get("success")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().casefold() in {"true", "1", "yes", "ok", "success"}


def _candidate_site(torrent: dict[str, Any], site_name: str, sites: list[dict[str, Any]]) -> dict[str, Any] | None:
    site_id = to_int(torrent.get("_site_id") or 0)
    return next((site for site in sites if site_id and to_int(site["id"]) == site_id), None) or next(
        (site for site in sites if str(site["name"]) == site_name), None,
    )


def _submission_torrent(torrent: dict[str, Any], site: dict[str, Any] | None) -> dict[str, Any]:
    """交给 MoviePilot 的种子信息：补上站点当前的 Cookie 与 UA。"""
    payload = dict(torrent)
    if site and str(site.get("adapter")) in COOKIE_ADAPTERS:
        payload["site_cookie"] = str(site.get("cookie") or "")
        payload["site_ua"] = str(site.get("user_agent") or payload.get("site_ua") or "")
    return payload


def _matches_active_torrent(candidate: Any, item: dict[str, Any], active_torrents: list[dict[str, Any]]) -> bool:
    """Return True when the candidate's release already has an active Transmission task."""
    candidate_name = normalized_download_name(candidate["title"])
    for torrent in active_torrents:
        torrent_name = normalized_download_name(first_value(torrent, ("name", "torrent_name"), ""))
        if candidate_name and torrent_name and candidate_name == torrent_name:
            return True
        if torrent_matches_item(item, str(first_value(torrent, ("name", "torrent_name"), ""))):
            return True
    return False

@router.post("/api/selection/items/{candidate_id}", response_model=SelectionToggled)
async def toggle_selection(candidate_id: str) -> dict[str, Any]:
    prune_raw_candidates()
    with connect() as conn:
        # Serialize the check-and-insert across workers; the candidate primary
        # key alone cannot protect the same release represented by two rows.
        conn.execute("BEGIN IMMEDIATE")
        candidate = queries.selection_candidate(conn, candidate_id)
        if not candidate:
            raise HTTPException(404, "候选不存在")
        if queries.is_selected(conn, candidate_id):
            queries.remove_from_selection(conn, candidate_id)
            return {"candidate_id": candidate_id, "in_selection": False}
        if candidate["eligibility"] != "eligible":
            raise HTTPException(422, f"该资源已被电影策略排除：{candidate['exclusion_reason'] or '不符合允许组合'}")
        if candidate_id not in raw_candidates:
            raise HTTPException(409, "该候选的搜索上下文已失效，请重新寻片后再加入待入馆清单")
        # 同一影片、同一站点、同一发布已在清单中时不重复加入，避免跨任务重复提交。
        if queries.same_release_selected(conn, candidate["playlist_item_id"], candidate["site_name"], candidate["resource_key"]):
            raise HTTPException(422, "该发布已在待入馆清单中（相同影片与站点），请先移除现有条目")
        queries.add_to_selection(conn, candidate_id)
        return {"candidate_id": candidate_id, "in_selection": True}

@router.get("/api/selection", response_model=list[SelectionItem])
async def selection() -> list[dict[str, Any]]:
    prune_raw_candidates()
    with connect() as conn:
        items = queries.selection_items(conn)
    for item in items:
        item["detail_url"] = safe_detail_url(item.get("detail_url"))
        item["context_available"] = item["id"] in raw_candidates
    return items

@router.post("/api/selection/submit", response_model=SubmitResult)
async def submit_selection() -> dict[str, Any]:
    prune_raw_candidates()
    if selection_submit_lock.locked():
        raise HTTPException(409, "待入馆清单正在提交，请勿重复操作")
    async with selection_submit_lock:
        with connect() as conn:
            rows = queries.selection_for_submission(conn)
        if not rows:
            raise HTTPException(422, "待入馆清单为空")
        transmission = TransmissionClient()
        try:
            current_torrents = await asyncio.wait_for(transmission.current_downloads(), timeout=6)
        except Exception as exc:
            raise HTTPException(503, "无法确认 Transmission 当前下载任务，已暂停提交") from exc
        # 停滞超过 24 小时、没有做种者的种子不算“正在下载”：允许为同一影片换一个资源再提交。
        active_torrents = [
            torrent for torrent in current_torrents if is_transmission_downloading(torrent) and not long_stalled(torrent)
        ]
        with connect() as conn:
            sites = all_sites(conn)
        emby = EmbyClient()
        moviepilot, completed, needs_research, submitted_tasks, expired_items = MoviePilotClient(), 0, 0, [], []
        try:
            downloader = await moviepilot.transmission_downloader()
        except Exception as exc:
            raise HTTPException(503, f"无法确认 MoviePilot 的 Transmission 下载器，已暂停提交：{safe_error(exc, 200)}") from exc
        skipped: list[dict[str, Any]] = []
        blocked_unknown: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        blocked_site: list[dict[str, Any]] = []
        for candidate in rows:
            item_snapshot = playlist_item_snapshot({
                "id": candidate["playlist_snapshot_id"],
                "imdb_id": candidate["playlist_imdb_id"],
                "original_title": candidate["playlist_original_title"],
                "chinese_title": candidate["playlist_chinese_title"],
                "year": candidate["playlist_year"],
                "tmdb_id": candidate["playlist_tmdb_id"],
                "tmdb_title": candidate["playlist_tmdb_title"],
                "tmdb_original_title": candidate["playlist_tmdb_original_title"],
                "tmdb_year": candidate["playlist_tmdb_year"],
                "tmdb_imdb_id": candidate["playlist_tmdb_imdb_id"],
                "library_state": candidate["playlist_library_state"],
                "library_checked_at": candidate["playlist_library_checked_at"],
            })
            raw = raw_candidates.get(candidate["id"])
            if not raw:
                needs_research += 1
                expired_items.append({"candidate_id": candidate["id"], "title": candidate["playlist_original_title"]})
                message = "搜索上下文已失效，请重新寻片后加入待入馆清单"
                resource_key = candidate["resource_key"] or resource_fingerprint(candidate["title"], candidate["size"])
                with connect() as conn:
                    if not queries.failure_recorded(conn, candidate["id"], message):
                        queries.record_submission(
                            conn, candidate, snapshot_json=json_value(item_snapshot), resource_key=resource_key,
                            success=False, message=message,
                        )
                continue
            # 幂等复查 1：相同发布已成功提交过（同候选或同影片+站点+资源指纹），跳过避免重复下载。
            resource_key = candidate["resource_key"] or resource_fingerprint(candidate["title"], candidate["size"])
            with connect() as conn:
                already_submitted = queries.release_already_submitted(
                    conn, candidate["id"], resource_key, candidate["playlist_item_id"], candidate["site_name"],
                )
            if already_submitted:
                skipped.append({"candidate_id": candidate["id"], "title": candidate["playlist_original_title"], "reason": "该发布已提交过"})
                continue
            # 幂等复查 2：相同发布已在 Transmission 下载中时跳过；查询失败已在提交前阻断。
            playlist_item = {
                "id": candidate["playlist_snapshot_id"], "imdb_id": candidate["playlist_imdb_id"],
                "original_title": candidate["playlist_original_title"], "chinese_title": candidate["playlist_chinese_title"],
                "year": candidate["playlist_year"], "tmdb_id": candidate["playlist_tmdb_id"],
                "tmdb_title": candidate["playlist_tmdb_title"], "tmdb_original_title": candidate["playlist_tmdb_original_title"],
                "tmdb_year": candidate["playlist_tmdb_year"], "tmdb_imdb_id": candidate["playlist_tmdb_imdb_id"],
            }
            if _matches_active_torrent(candidate, playlist_item, active_torrents):
                skipped.append({"candidate_id": candidate["id"], "title": candidate["playlist_original_title"], "reason": "Transmission 正在下载"})
                continue
            # 幂等复查 3：每次提交前都重新确认 Emby，避免旧的 not_found
            # 状态在 Emby 短暂故障时被误当成“肯定未入库”。
            state, _, _ = await library_details(
                emby,
                str(candidate["playlist_tmdb_title"] or candidate["playlist_chinese_title"] or candidate["playlist_original_title"]),
                candidate["playlist_tmdb_year"] or candidate["playlist_year"],
                candidate["playlist_tmdb_id"], candidate["playlist_tmdb_imdb_id"] or candidate["playlist_imdb_id"],
            )
            if state == "in_library":
                skipped.append({"candidate_id": candidate["id"], "title": candidate["playlist_original_title"], "reason": "影片已入库"})
                continue
            if state == "unknown":
                blocked_unknown.append({"candidate_id": candidate["id"], "title": candidate["playlist_original_title"], "reason": "无法确认 Emby 媒体库状态"})
                continue
            # 幂等复查 4：候选可能已存放数天，提交前回站点确认种子没有被删除。
            site = _candidate_site(raw.get("torrent") or {}, str(candidate["site_name"] or ""), sites)
            if site and str(site.get("adapter")) == "nexusphp":
                try:
                    await wait_for_site_rate_limit(site)
                    detail_url = candidate["detail_url"] or (raw.get("torrent") or {}).get("detail_url")
                    # Cookie 失效时补拉 CookieCloud 并重试一次；新 Cookie 同时用于随后交给 MoviePilot 的种子。
                    enclosure = (raw.get("torrent") or {}).get("enclosure")
                    present, fresh_link = await with_cookie_refresh(
                        site, lambda current: verify_and_refresh(current, detail_url, enclosure),
                    )
                    # 站点K等站点的下载地址带时效签名（约一小时），用详情页上当前有效的地址提交。
                    if present and is_signed_download(enclosure):
                        if not fresh_link:
                            raise RuntimeError("下载地址已过期，详情页上没有找到新的下载地址")
                        raw = {**raw, "torrent": {**raw["torrent"], "enclosure": fresh_link}}
                except Exception as exc:
                    blocked_site.append({
                        "candidate_id": candidate["id"], "title": candidate["playlist_original_title"],
                        "reason": f"无法确认种子仍在{site['name']}：{safe_error(exc, 200)}",
                    })
                    continue
                if present is False:
                    with connect() as conn:
                        queries.exclude_deleted_on_site(conn, candidate["id"], SITE_DELETED_REASON)
                    forget_raw_candidate(candidate["id"])
                    removed.append({"candidate_id": candidate["id"], "title": candidate["playlist_original_title"], "reason": SITE_DELETED_REASON})
                    continue
            try:
                if not raw.get("media"):
                    raise RuntimeError("缺少媒体信息，无法应用 MoviePilot 分类规则")
                # 固定走 MoviePilot：按 TMDB 编号识别影片与分类、按分类目录选择下载路径，
                # 再交由 Transmission 写入 MOVIEPILOT 与站点标签，供 MP 后续整理。
                # 片单影片可能在寻片后重新识别过：以片单当前的 TMDB 编号为准，交给 MoviePilot 识别分类。
                media = {**raw["media"], "tmdb_id": candidate["playlist_tmdb_id"] or raw["media"].get("tmdb_id")}
                response = await moviepilot.download(media, _submission_torrent(raw["torrent"], site), downloader=downloader)
                success = _moviepilot_success(response)
                if not isinstance(response, dict):
                    message = "MoviePilot 返回格式无效，未确认提交成功"
                    submission_hash = None
                elif success:
                    message = response.get("message") or response.get("hash")
                    # MoviePilot 把 Transmission 里的种子 hash 放在 data.download_id（旧版本为顶层 hash）。
                    data = response.get("data") if isinstance(response.get("data"), dict) else {}
                    submission_hash = str(response.get("hash") or data.get("download_id") or "").strip() or None
                    submitted_tasks.append({"candidate_id": candidate["id"], "hash": submission_hash, "mode": "moviepilot"})
                else:
                    message = response.get("message") or "MoviePilot 未确认提交成功"
                    submission_hash = None
                message = sanitize_sensitive_text(message) if message else None
            except Exception as exc:
                success, message, submission_hash = False, safe_error(exc), None
            with connect() as conn:
                queries.record_submission(
                    conn, candidate, snapshot_json=json_value(item_snapshot), resource_key=resource_key,
                    success=success, message=message, submission_hash=submission_hash,
                )
                if success:
                    queries.mark_submitted(conn, candidate["id"])
                    completed += 1
            if success:
                # 事务提交后再删上下文，避免在同一数据库上等待自己的写锁。
                forget_raw_candidate(candidate["id"])
        if needs_research and completed == 0 and not (skipped or removed or blocked_site):
            raise HTTPException(409, f"待入馆清单中 {needs_research} 个资源的搜索上下文已失效，请重新寻片后加入")
        failed = len(rows) - completed - len(skipped) - needs_research - len(blocked_unknown) - len(removed) - len(blocked_site)
        event_logger().info("download_submit", extra={
            "submitted": completed, "skipped": len(skipped), "blocked_unknown": len(blocked_unknown), "needs_research": needs_research, "failed": failed,
            "removed": len(removed), "blocked_site": len(blocked_site),
            "skipped_reasons": sorted({str(item.get("reason")) for item in skipped}),
        })
        return {
            "submitted": completed, "needs_research": needs_research, "expired_items": expired_items,
            "skipped": skipped, "blocked_unknown": blocked_unknown, "removed": removed, "blocked_site": blocked_site,
            "mode": "moviepilot", "tasks": submitted_tasks,
        }
