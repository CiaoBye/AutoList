"""Title matching and candidate identity checks."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from typing import Any


def canonical_item_title(item: sqlite3.Row | dict[str, Any]) -> str:
    return str(item["tmdb_title"] or item["chinese_title"] or item["original_title"])


def canonical_item_original_title(item: sqlite3.Row | dict[str, Any]) -> str:
    return str(item["tmdb_original_title"] or item["original_title"])


def canonical_item_year(item: sqlite3.Row | dict[str, Any]) -> int | None:
    value = item["tmdb_year"] or item["year"]
    return int(value) if value else None


def normalized_title_key(title: str) -> str:
    """统一标题归一化：去除标点/空白后小写折叠（审计 2-6）。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(title or "").casefold())


def item_identity_keys(value: dict[str, Any]) -> list[tuple[str, str]]:
    """影片身份键（imdb/tmdb/title+year），全仓统一归一化规则（审计 2-6）。"""
    keys: list[tuple[str, str]] = []
    if value.get("imdb_id"):
        keys.append(("imdb", str(value["imdb_id"]).casefold()))
    for field in ("tmdb_id", "source_tmdb_id"):
        if value.get(field):
            keys.append(("tmdb", str(value[field])))
    normalized_title = normalized_title_key(value.get("original_title"))
    if normalized_title:
        keys.append(("title", f"{normalized_title}:{value.get('year') or ''}"))
    return keys


def fold_accents(value: Any) -> str:
    """去掉字母上的重音（Cléo → Cleo、À bout → A bout），种子标题多用不带重音的写法。"""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalized_title_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", fold_accents(value).casefold())


def title_tokens(value: Any) -> set[str]:
    return {
        token for token in re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", fold_accents(value).casefold()).split()
        if len(token) > 1 or token.isdigit()
    }


def meaningful_title(value: Any) -> bool:
    """去掉标点与无法比对的文字（如片假名）后还剩非纯数字的内容才算片名，“シークレット・サンシャイン：2007”只剩 2007，不算。"""
    normalized = normalized_title_text(value)
    return bool(normalized) and not normalized.isdigit()


def item_title_variants(item: sqlite3.Row | dict[str, Any]) -> list[str | None]:
    """比对种子标题时认可的片名：TMDB 中文名与原名、导入原名、中文名，以及 TMDB 的其他译名（如 8½ 的 Eight and a Half）。"""
    identity = dict(item)
    variants: list[str | None] = [
        identity.get("tmdb_original_title"), identity.get("tmdb_title"),
        identity.get("original_title"), identity.get("chinese_title"),
    ]
    try:
        alternatives = json.loads(identity.get("tmdb_alt_titles_json") or "[]")
    except (TypeError, ValueError):
        alternatives = []
    if isinstance(alternatives, list):
        variants.extend(str(title) for title in alternatives if isinstance(title, str))
    return variants


# 合集只认明确的写法：三部曲、套装、“某某电影合集”、年份区间（1972-1990）与中文的合集 / 三部曲 / 全集。
# Criterion Collection（CC 标准收藏版）、REPACK 与中文介绍里的“作品系列”都是单片，不算合集。
COLLECTION_PATTERN = re.compile(
    r"(?i)(?<![a-z])(?:trilogy|quadrilogy|duology|anthology)(?![a-z])"
    r"|(?<![a-z])box[ ._-]*set(?![a-z])"
    r"|(?<![a-z])(?:films?|movies?)[ ._-]+collection(?![a-z])"
    r"|(?<![a-z])complete[ ._-]+(?:collection|series|films?|movies?)(?![a-z])"
    r"|(?<!\d)(?:19|20)\d{2}[ ._]*[-–~][ ._]*(?:19|20)\d{2}(?!\d)"
    r"|合集|三部曲|四部曲|全集"
)


def is_collection_title(torrent_title: str) -> bool:
    return bool(COLLECTION_PATTERN.search(torrent_title))


TITLE_STOP_WORDS = {
    "a", "an", "and", "at", "by", "da", "das", "de", "del", "der", "die", "di", "dos",
    "for", "from", "in", "la", "le", "les", "of", "on", "or", "the", "to", "un", "una",
    "upon", "with", "once", "time",
}


def informative_title_tokens(value: Any) -> set[str]:
    return title_tokens(value) - TITLE_STOP_WORDS


YEAR_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def item_years(item: sqlite3.Row | dict[str, Any], media: dict[str, Any] | None = None) -> set[int]:
    """影片可能的年份：TMDB 年份与片单来源年份。两者常差一年（首映与上映地区不同，如卡萨布兰卡 1942 / 1943）。"""
    identity = dict(item)
    values = (media or {}).get("year"), identity.get("tmdb_year"), identity.get("year")
    return {int(str(value)[:4]) for value in values if value and str(value)[:4].isdigit()}


def year_conflict(target_years: set[int], torrent_title: str) -> str | None:
    """资源标题里的年份与影片年份相差超过一年时返回冲突说明；标题不含年份不算冲突。"""
    years = {int(value) for value in YEAR_PATTERN.findall(torrent_title)}
    if not target_years or not years:
        return None
    if any(abs(year - target) <= 1 for year in years for target in target_years):
        return None
    targets = " / ".join(str(year) for year in sorted(target_years))
    return f"年份不匹配：目标 {targets}，资源包含 {', '.join(str(year) for year in sorted(years))}"


def _torrent_year_conflicts(item: sqlite3.Row | dict[str, Any], torrent_title: str) -> bool:
    return year_conflict(item_years(item), torrent_title) is not None


def strict_torrent_matches_item(item: sqlite3.Row | dict[str, Any], torrent_title: str) -> bool:
    """Require a meaningful title token so shared words cannot identify another movie."""
    candidate = normalized_title_text(torrent_title)
    candidate_tokens = title_tokens(torrent_title)
    if not candidate or not candidate_tokens or _torrent_year_conflicts(item, torrent_title):
        return False
    variants = item_title_variants(item)
    for variant in variants:
        if not meaningful_title(variant):
            continue
        normalized = normalized_title_text(variant)
        tokens = informative_title_tokens(variant)
        if tokens and tokens.issubset(candidate_tokens):
            return True
        # 子串回退要求词边界：复数/长尾变体（如 The Godfathers）不再命中 The Godfather。
        if normalized and re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z])", candidate) \
                and (not tokens or not any(token.isdigit() for token in tokens)):
            return True
    return False


def torrent_matches_item(item: sqlite3.Row | dict[str, Any], torrent_title: str) -> bool:
    candidate = normalized_title_text(torrent_title)
    if not candidate or _torrent_year_conflicts(item, torrent_title):
        return False
    variants = item_title_variants(item)
    candidate_tokens = title_tokens(torrent_title)
    for variant in variants:
        if not meaningful_title(variant):
            continue
        normalized = normalized_title_text(variant)
        tokens = title_tokens(variant)
        if tokens and tokens.issubset(candidate_tokens):
            return True
        if normalized and re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z])", candidate) \
                and (not tokens or not any(token.isdigit() for token in tokens)):
            return True
    return False


def candidate_identity(
    item: sqlite3.Row | dict[str, Any], media: dict[str, Any], torrent_title: str, torrent_imdb: str | None = None,
) -> tuple[bool, str | None]:
    """资源是否就是目标影片。站点行带 IMDb 编号时以编号为准：不一致直接排除，一致则不再比对年份与片名。"""
    keys = item.keys() if hasattr(item, "keys") else ()
    target_imdb = str(
        media.get("imdb_id") or (item["tmdb_imdb_id"] if "tmdb_imdb_id" in keys else None)
        or (item["imdb_id"] if "imdb_id" in keys else None) or ""
    ).strip().lower()
    torrent_imdb = str(torrent_imdb or "").strip().lower()
    is_collection = is_collection_title(torrent_title)
    if target_imdb and torrent_imdb:
        if torrent_imdb != target_imdb:
            return False, f"IMDb 编号不匹配：目标 {target_imdb}，资源为 {torrent_imdb}"
        # 合集常带第一部的 IMDb 编号（如“教父 I-III 合集”），编号一致也要排除。
        return (False, "疑似合集或系列资源") if is_collection else (True, None)
    conflict = year_conflict(item_years(item, media), torrent_title)
    if conflict:
        return False, conflict
    if is_collection:
        return False, "疑似合集或系列资源"
    # 续集/分卷拦截：仅当目标片名本身不含序号词时生效，
    # 避免“The Godfather Part II”这类正式片名被自己的种子标题拦截。
    target_text = " ".join(str(item[key] or "") for key in ("tmdb_original_title", "tmdb_title", "original_title", "chinese_title"))
    if not re.search(r"(?i)\b(?:part|vol\.?|volume|chapter|episode)\b", target_text):
        if re.search(r"(?i)\b(?:part|vol\.?|volume|chapter|episode)[.\s_-]*(?:[0-9]+|[ivx]+)\b", torrent_title):
            return False, "疑似续集或分卷资源"
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
