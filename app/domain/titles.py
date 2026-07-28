"""Title matching and candidate identity checks."""

from __future__ import annotations

import re
import sqlite3
from typing import Any


def canonical_item_title(item: sqlite3.Row | dict[str, Any]) -> str:
    return str(item["tmdb_title"] or item["chinese_title"] or item["original_title"])


def canonical_item_original_title(item: sqlite3.Row | dict[str, Any]) -> str:
    return str(item["tmdb_original_title"] or item["original_title"])


def canonical_item_year(item: sqlite3.Row | dict[str, Any]) -> int | None:
    value = item["tmdb_year"] or item["year"]
    return int(value) if value else None


def normalized_title_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").casefold())


def title_tokens(value: Any) -> set[str]:
    return {
        token for token in re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", str(value or "").casefold()).split()
        if len(token) > 1 or token.isdigit()
    }


TITLE_STOP_WORDS = {
    "a", "an", "and", "at", "by", "da", "das", "de", "del", "der", "die", "di", "dos",
    "for", "from", "in", "la", "le", "les", "of", "on", "or", "the", "to", "un", "una",
    "upon", "with", "once", "time",
}


def informative_title_tokens(value: Any) -> set[str]:
    return title_tokens(value) - TITLE_STOP_WORDS


def strict_torrent_matches_item(item: sqlite3.Row | dict[str, Any], torrent_title: str) -> bool:
    """Require a meaningful title token so shared words cannot identify another movie."""
    candidate = normalized_title_text(torrent_title)
    candidate_tokens = title_tokens(torrent_title)
    if not candidate or not candidate_tokens:
        return False
    variants = [
        item["tmdb_original_title"] if item["tmdb_original_title"] else None,
        item["tmdb_title"] if item["tmdb_title"] else None,
        item["original_title"], item["chinese_title"] if item["chinese_title"] else None,
    ]
    for variant in variants:
        normalized = normalized_title_text(variant)
        tokens = informative_title_tokens(variant)
        if tokens and tokens.issubset(candidate_tokens):
            return True
        if normalized and normalized in candidate and (not tokens or not any(token.isdigit() for token in tokens)):
            return True
    return False


def torrent_matches_item(item: sqlite3.Row | dict[str, Any], torrent_title: str) -> bool:
    candidate = normalized_title_text(torrent_title)
    if not candidate:
        return False
    variants = [
        item["tmdb_original_title"] if item["tmdb_original_title"] else None,
        item["tmdb_title"] if item["tmdb_title"] else None,
        item["original_title"], item["chinese_title"] if item["chinese_title"] else None,
    ]
    candidate_tokens = title_tokens(torrent_title)
    for variant in variants:
        normalized = normalized_title_text(variant)
        tokens = title_tokens(variant)
        if tokens and tokens.issubset(candidate_tokens):
            return True
        if normalized and normalized in candidate and (not tokens or not any(token.isdigit() for token in tokens)):
            return True
    return False


def candidate_identity(item: sqlite3.Row | dict[str, Any], media: dict[str, Any], torrent_title: str) -> tuple[bool, str | None]:
    target_year = str(media.get("year") or canonical_item_year(item) or "").strip()
    years = set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", torrent_title))
    if target_year and years and target_year not in years:
        return False, f"年份不匹配：目标 {target_year}，资源包含 {', '.join(sorted(years))}"
    if re.search(r"(?i)(?:trilogy|collection|box[ ._-]*set|complete|pack|合集|系列|全集)", torrent_title):
        return False, "疑似合集或系列资源"
    if not strict_torrent_matches_item(item, torrent_title):
        return False, "片名不匹配：资源片名与目标影片不一致"
    return True, None


def normalized_download_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").casefold())


def is_transmission_downloading(torrent: dict[str, Any]) -> bool:
    status = torrent.get("status")
    try:
        status_value = int(status)
    except (TypeError, ValueError):
        status_value = -1
    try:
        percent_done = float(torrent.get("percentDone") or 0)
    except (TypeError, ValueError):
        percent_done = 0
    return status_value in {1, 2, 3, 4} and percent_done < 1
