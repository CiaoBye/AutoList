"""TMDB recognition and candidate policy analysis helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from ..candidate_policy import analyze as analyze_policy_candidate
from ..clients import AIRecognitionClient, TMDBClient
from ..database import connect
from ..util import to_int, utc_now


def analyze_candidate(
    title: str, index: int, config: dict[str, str], torrent: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """策略可复用：调用方已解析时传入 policy，避免每次候选重复 json.loads（审计 2-13）。"""
    if policy is None:
        try:
            policy = json.loads(config.get("candidate_policy") or "{}")
        except (TypeError, json.JSONDecodeError):
            policy = {}
    return analyze_policy_candidate(title, index, policy, torrent)


def extract_contexts(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        data = response.get("data", response.get("result", []))
    else:
        data = response
    if isinstance(data, dict):
        data = data.get("contexts", data.get("items", []))
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def extract_pair(context: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]] | None:
    media = context.get("media_info") or context.get("media")
    torrent = context.get("torrent_info") or context.get("torrent")
    if isinstance(torrent, dict):
        return media if isinstance(media, dict) else None, torrent
    if any(context.get(key) for key in ("enclosure", "download_url", "magnet")):
        return None, context
    return None


def select_tmdb_match(options: list[dict[str, Any]], title: str, year: int | None) -> dict[str, Any] | None:
    if not options:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    for item in options:
        item_year = str(item.get("release_date") or "")[:4]
        names = (item.get("title"), item.get("original_title"))
        normalized_names = {re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip() for name in names if name}
        if normalized in normalized_names and (not year or item_year == str(year)):
            return item
    same_year = [item for item in options if not year or str(item.get("release_date") or "")[:4] == str(year)]
    return same_year[0] if same_year else options[0]


async def recognize_movie(title: str, year: int | None, imdb_id: str | None = None) -> dict[str, Any] | None:
    tmdb = TMDBClient()
    async def enriched(match: dict[str, Any] | None) -> dict[str, Any] | None:
        if not match:
            return None
        result = dict(match)
        resolved_imdb = imdb_id
        if not resolved_imdb and result.get("id"):
            try:
                resolved_imdb = (await tmdb.movie_external_ids(int(result["id"]))).get("imdb_id")
            except Exception:
                resolved_imdb = None
        result["imdb_id"] = resolved_imdb
        return result
    if imdb_id:
        match = select_tmdb_match(await tmdb.find_by_imdb(imdb_id), title, year)
        if match:
            return await enriched(match)
    options = await tmdb.search_movie(title, year)
    match = select_tmdb_match(options, title, year)
    if match:
        return await enriched(match)
    suggestion = await AIRecognitionClient().suggest(title, year)
    if not suggestion:
        return None
    options = await tmdb.search_movie(str(suggestion.get("original_title") or suggestion.get("title") or title), suggestion.get("year") or year)
    return await enriched(select_tmdb_match(
        options, str(suggestion.get("original_title") or suggestion.get("title") or title), suggestion.get("year") or year,
    ))


def tmdb_item_values(media: dict[str, Any], fallback_imdb: str | None = None) -> tuple[Any, ...]:
    release_year = str(media.get("release_date") or "")[:4]
    return (
        to_int(media["id"]), str(media.get("title") or "").strip() or None,
        str(media.get("original_title") or "").strip() or None,
        to_int(release_year) if release_year.isdigit() else None,
        media.get("imdb_id") or fallback_imdb, utc_now(),
    )


def persist_tmdb_item(item_id: int, media: dict[str, Any], fallback_imdb: str | None = None) -> None:
    values = tmdb_item_values(media, fallback_imdb)
    with connect() as conn:
        conn.execute(
            """UPDATE playlist_items
               SET tmdb_id=?,tmdb_title=?,tmdb_original_title=?,tmdb_year=?,tmdb_imdb_id=?,tmdb_checked_at=?
               WHERE id=?""",
            (*values, item_id),
        )

