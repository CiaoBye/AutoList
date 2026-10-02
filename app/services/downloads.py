"""下载页：Transmission 里全部种子的状态，能对上提交记录的附上影片（只读，不操作 Transmission）。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..clients import MoviePilotClient, TransmissionClient
from ..config import settings
from ..database import connect
from ..domain.titles import normalized_download_name, strict_torrent_matches_item
from ..queries import downloads as queries
from ..security import sanitize_sensitive_text, signed_media_url
from ..util import to_int, utc_now
from .films import poster_url
from .history import transfer_info
from .recognition import TMDB_POSTER_PATH

# 列表排序：正在动的在前，其次需要处理的，再排队，最后已完成的。
STATE_ORDER = {
    "downloading": 0, "checking": 1, "stalled": 2, "error": 3, "queued": 4, "paused": 5, "seeding": 6, "completed": 7,
}
STATES = tuple(STATE_ORDER)
# MoviePilot 写入的标签：MOVIEPILOT 与整理后的“已整理”，都不是站点名。
SKIP_LABELS = {"moviepilot", "已整理"}


def torrent_state(torrent: dict[str, Any]) -> dict[str, Any]:
    """在 ``transfer_info`` 的基础上补上已下完的状态：做种中、已完成（已停止做种）。"""
    info = transfer_info(torrent)
    try:
        finished = float(torrent.get("percentDone") or 0) >= 1
    except (TypeError, ValueError):
        finished = False
    if finished and info["state"] != "error":
        status = to_int(torrent.get("status"))
        info = {**info, "state": "seeding" if status in (5, 6) else ("checking" if status in (1, 2) else "completed"),
                "eta_seconds": None, "percent": 100.0}
    return info


def _iso(epoch: Any) -> str | None:
    value = to_int(epoch)
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value > 0 else None


def _site(labels: Any) -> str | None:
    """MoviePilot 提交时把站点名写进标签（另一个标签是 MOVIEPILOT）。"""
    for label in labels or []:
        if str(label).strip() and str(label).strip().casefold() not in SKIP_LABELS:
            return str(label).strip()
    return None


def _film_index() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """成功提交过的记录按种子 hash 与名称索引到片单影片，另给出提交过的影片列表供按片名年份兜底。"""
    with connect() as conn:
        rows = queries.submitted_films(conn)
    by_hash: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    films: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        submission_hash = str(item.pop("submission_hash") or "").strip().casefold()
        name = normalized_download_name(item.pop("torrent_name"))
        if submission_hash:
            by_hash.setdefault(submission_hash, item)
        if name:
            by_name.setdefault(name, item)
        films.setdefault(to_int(item["id"]), item)
    return by_hash, by_name, list(films.values())


def _match_film(
    torrent: dict[str, Any], by_hash: dict[str, dict[str, Any]], by_name: dict[str, dict[str, Any]],
    films: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """先按种子 hash，再按名称；都对不上时按片名与年份，只有唯一一部影片匹配才算。"""
    film = by_hash.get(str(torrent.get("hashString") or "").strip().casefold()) \
        or by_name.get(normalized_download_name(torrent.get("name")))
    if film:
        return film
    name = str(torrent.get("name") or "")
    found = [item for item in films if strict_torrent_matches_item(item, name)]
    return found[0] if len(found) == 1 else None


# 不是 AutoList 提交的种子：先查 MoviePilot 下载历史（按 hash），再按种子名识别；结果缓存在 torrent_media。
HISTORY_PAGE_SIZE = 100
HISTORY_MAX_PAGES = 5
RECOGNIZE_PER_REQUEST = 6
UNKNOWN_RETRY = timedelta(days=7)
IDENTIFY_TIMEOUT_SECONDS = 8
HASH_PATTERN = re.compile(r"[0-9a-f]{40}")


def tmdb_poster_path(value: Any) -> str | None:
    """MoviePilot 给的海报可能是 TMDB 路径（/abc.jpg）或完整图片地址，统一取出路径。"""
    text = str(value or "").strip()
    if "image.tmdb.org" in text:
        text = "/" + text.rstrip("/").rsplit("/", 1)[-1]
    return text if TMDB_POSTER_PATH.fullmatch(text) else None


def _history_tmdb_id(record: dict[str, Any]) -> int | None:
    """旧版 MoviePilot 的下载历史写 tmdbid；新版改为 media_source=themoviedb + media_id（字符串）。"""
    if record.get("tmdbid"):
        return to_int(record["tmdbid"]) or None
    if str(record.get("media_source") or "").casefold() == "themoviedb":
        return to_int(record.get("media_id")) or None
    return None


def _year(value: Any) -> int | None:
    match = re.match(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def _needs_lookup(cached: dict[str, Any] | None, now: datetime) -> bool:
    if cached is None:
        return True
    if cached["source"] != "none":
        return False
    try:
        return now - datetime.fromisoformat(cached["checked_at"]) > UNKNOWN_RETRY
    except (TypeError, ValueError):
        return True


async def identify_torrents(torrents: list[dict[str, Any]]) -> None:
    """为对不上片单影片的种子找出影片信息并缓存；每次请求只识别少量，其余在之后的刷新里继续。"""
    named = {
        str(torrent.get("hashString") or "").lower(): str(torrent.get("name") or "")
        for torrent in torrents if HASH_PATTERN.fullmatch(str(torrent.get("hashString") or "").lower())
    }
    with connect() as conn:
        cached = queries.torrent_media(conn, list(named))
    now = datetime.now(timezone.utc)
    wanted = {torrent_hash: name for torrent_hash, name in named.items() if _needs_lookup(cached.get(torrent_hash), now)}
    if not wanted or not (settings.mp_base_url and settings.mp_api_key):
        return
    client = MoviePilotClient()
    try:
        for page in range(1, HISTORY_MAX_PAGES + 1):
            records = await client.download_history(page, HISTORY_PAGE_SIZE)
            for record in records:
                torrent_hash = str(record.get("download_hash") or "").lower()
                if torrent_hash in wanted and record.get("title"):
                    with connect() as conn:
                        queries.remember_torrent_media(
                            conn, torrent_hash, title=str(record["title"]), year=_year(record.get("year")),
                            tmdb_id=_history_tmdb_id(record), poster_path=tmdb_poster_path(record.get("poster")),
                            source="moviepilot",
                        )
                    wanted.pop(torrent_hash)
            if not wanted or len(records) < HISTORY_PAGE_SIZE:
                break
    except Exception:
        pass
    slots = asyncio.Semaphore(3)

    async def recognize(torrent_hash: str, name: str) -> None:
        async with slots:
            try:
                media = await client.recognize(name)
            except Exception:
                return
        with connect() as conn:
            queries.remember_torrent_media(
                conn, torrent_hash,
                title=str(media["title"]) if media else None, year=_year(media.get("year")) if media else None,
                tmdb_id=to_int(media.get("tmdb_id")) or None if media else None,
                poster_path=tmdb_poster_path(media.get("poster_path")) if media else None,
                source="recognize" if media else "none",
            )

    await asyncio.gather(*(recognize(torrent_hash, name) for torrent_hash, name in list(wanted.items())[:RECOGNIZE_PER_REQUEST]))


def download_item(torrent: dict[str, Any], film: dict[str, Any] | None, media: dict[str, Any] | None = None) -> dict[str, Any]:
    info = torrent_state(torrent)
    torrent_hash = str(torrent.get("hashString") or "").lower()
    # 不在片单里但识别出了影片：显示片名、年份与海报，不链接影片详情。
    known = media if not film and media and media.get("title") else None
    size = to_int(torrent.get("sizeWhenDone") or torrent.get("totalSize"))
    left = to_int(torrent.get("leftUntilDone"))
    ratio = torrent.get("uploadRatio")
    return {
        "hash": str(torrent.get("hashString") or ""),
        "name": str(torrent.get("name") or ""),
        "film_id": to_int(film["id"]) if film else None,
        "film_title": str(film.get("tmdb_title") or film.get("chinese_title") or film.get("original_title")) if film
        else (str(known["title"]) if known else None),
        "film_year": (to_int(film.get("tmdb_year") or film.get("year")) or None) if film else (known.get("year") if known else None),
        "poster_url": poster_url(film) if film else (
            signed_media_url(f"/api/downloads/{torrent_hash}/poster") if known and known.get("poster_path") else None
        ),
        "site": (film.get("submitted_site") if film else None) or _site(torrent.get("labels")),
        "state": info["state"],
        "percent": info["percent"],
        "size": size,
        "downloaded": max(0, size - left) if size else 0,
        "rate_down": info["rate_bps"],
        "rate_up": to_int(torrent.get("rateUpload")),
        "eta_seconds": info["eta_seconds"],
        "seeders": info["peers"],
        "leechers": to_int(torrent.get("peersGettingFromUs")),
        "ratio": round(float(ratio), 2) if isinstance(ratio, (int, float)) and ratio >= 0 else None,
        "added_at": _iso(torrent.get("addedDate")),
        "done_at": _iso(torrent.get("doneDate")),
        "error": info["error"],
    }


async def downloads_overview() -> dict[str, Any]:
    client = TransmissionClient()
    if not client.base_url:
        return {"configured": False, "error": "未配置 Transmission", "items": [], "summary": None, "web_url": None,
                "checked_at": utc_now()}
    try:
        data = await asyncio.wait_for(client.overview(), timeout=10)
    except Exception as exc:
        return {"configured": True, "error": sanitize_sensitive_text(f"无法读取 Transmission：{exc}", 300), "items": [],
                "summary": None, "web_url": client.web_url(), "checked_at": utc_now()}
    by_hash, by_name, films = _film_index()
    matched = [(torrent, _match_film(torrent, by_hash, by_name, films)) for torrent in data["torrents"]]
    unknown = [torrent for torrent, film in matched if film is None]
    try:
        await asyncio.wait_for(identify_torrents(unknown), timeout=IDENTIFY_TIMEOUT_SECONDS)
    except Exception:
        pass
    with connect() as conn:
        media = queries.torrent_media(conn, [str(torrent.get("hashString") or "").lower() for torrent in unknown])
        # 不是经 AutoList 提交、但识别出的影片就在片单里（如手动下载的）：同样对上片单影片。
        listed = queries.films_by_tmdb(conn, sorted({to_int(entry["tmdb_id"]) for entry in media.values() if entry.get("tmdb_id")}))
    matched = [
        (torrent, film or listed.get(to_int((media.get(str(torrent.get("hashString") or "").lower()) or {}).get("tmdb_id"))))
        for torrent, film in matched
    ]
    items = [
        download_item(torrent, film, media.get(str(torrent.get("hashString") or "").lower()))
        for torrent, film in matched
    ]
    items.sort(key=lambda item: (STATE_ORDER[item["state"]], -(datetime.fromisoformat(item["added_at"]).timestamp() if item["added_at"] else 0)))
    counts = {state: sum(1 for item in items if item["state"] == state) for state in STATES}
    return {
        "configured": True, "error": None, "items": items, "web_url": client.web_url(), "checked_at": utc_now(),
        "summary": {
            "total": len(items), "counts": counts,
            "download_bps": to_int(data["download_bps"]), "upload_bps": to_int(data["upload_bps"]),
            "free_bytes": to_int(data["free_bytes"]) if data["free_bytes"] is not None else None,
        },
    }
