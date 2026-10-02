"""TMDB recognition and candidate policy analysis helpers."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..candidate_policy import analyze as analyze_policy_candidate
from ..clients import AIRecognitionClient, TMDBClient
from ..config import settings
from ..database import connect
from ..domain.titles import meaningful_title, normalized_title_text
from ..util import to_int, utc_now


# TMDB 图片路径形如 /kvJuiSJ....jpg；只接受这种形状，避免把任意 URL 片段拼进出站请求。
TMDB_POSTER_PATH = re.compile(r"/[A-Za-z0-9_-]{1,120}\.(?:jpg|jpeg|png|webp)")

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


def is_year_close(item_year: str | None, target_year: str | None, tolerance: int = 1) -> bool:
    if not item_year or not target_year:
        return True
    try:
        return abs(int(item_year) - int(target_year)) <= tolerance
    except ValueError:
        return False


def _normalized(value: Any) -> str:
    return re.sub(r"[^\w]+", " ", str(value or "").lower()).strip()


def _main_title(value: Any) -> str:
    """主标题：去掉冒号、破折号后的副标题（如 “M - Eine Stadt sucht einen Mörder” → “m”）。"""
    return _normalized(re.split(r"[:：]|\s+[-–—]\s+", str(value or ""), maxsplit=1)[0])


def _names(item: dict[str, Any]) -> set[str]:
    return {_normalized(name) for name in (item.get("title"), item.get("original_title")) if name} - {""}


def _year(item: dict[str, Any]) -> str:
    return str(item.get("release_date") or "")[:4]


def _title_overlaps(query: str, item: dict[str, Any]) -> bool:
    """片名部分重合：主标题一致，或较短片名按完整词包含于较长片名且覆盖其大部分词。

    只要求“包含”会把 “Cure” 配到 “Say It, Fight It, Cure It” 这类碰巧含有同一个词的影片。
    """
    query_main = _main_title(query)
    for name in (item.get("title"), item.get("original_title")):
        if not name:
            continue
        if query_main and query_main == _main_title(name):
            return True
        left, right = _normalized(query).split(), _normalized(name).split()
        shorter, longer = sorted((left, right), key=len)
        if not shorter or len(shorter) / len(longer) < 0.6:
            continue
        if f" {' '.join(shorter)} " in f" {' '.join(longer)} ":
            return True
    return False


def _most_voted(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """同一轮有多个候选时取评分人数最多的一部（同名翻拍、短片与正片并存很常见）。"""
    if not items:
        return None
    return max(items, key=lambda item: (to_int(item.get("vote_count")), float(item.get("popularity") or 0)))


def select_tmdb_match(options: list[dict[str, Any]], title: str, year: int | None) -> dict[str, Any] | None:
    if not options:
        return None
    normalized = _normalized(title)
    year_str = str(year) if year else None

    # 第一轮：规范化标题完全一致，且年份精确匹配（或未指定年份）
    exact = [item for item in options if normalized and normalized in _names(item)]
    match = _most_voted([item for item in exact if not year_str or _year(item) == year_str])
    if match:
        return match

    if year_str:
        # 第二轮：规范化标题完全一致，年份容差 ±1 年（电影节首映与公映跨年，如 Casablanca 1942 vs 1943）
        match = _most_voted([item for item in exact if is_year_close(_year(item), year_str, 1)])
        if match:
            return match

        # 第三轮：年份精确匹配，且主标题一致或片名大部分重合
        match = _most_voted([item for item in options if _year(item) == year_str and _title_overlaps(title, item)])
        if match:
            return match

        # 第四轮：TMDB 跨语言国际译名命中（如 Seven Samurai、Spirited Away）。
        # 片名无法直接比对时只信任 TMDB 首项，并要求年份足够确定：首项为非英语原片且年份精确一致，
        # 或者它是唯一一个年份相差不超过 1 年的候选；多个同年外语片并列时宁可不识别，避免误配。
        first = options[0]
        first_year = _year(first)
        orig_lang = str(first.get("original_language") or "").lower()
        close = [item for item in options if is_year_close(_year(item), year_str, 1)]
        if first_year and first in close:
            if orig_lang and orig_lang != "en" and first_year == year_str:
                return first
            if len(close) == 1:
                return first

    return None


LOCALIZED_FIELDS = ("title", "original_title", "release_date", "poster_path", "original_language")


async def recognize_movie(
    title: str, year: int | None, imdb_id: str | None = None, tmdb_id: int | None = None,
) -> dict[str, Any] | None:
    """识别顺序：IMDb 编号换 TMDB → 来源自带的 TMDB 编号 → 按片名搜索（本地化、英文，最后才用 AI 纠正片名）。

    返回结果带 ``matched_by``（imdb / tmdb / title），校准时只采信按编号得到的结果。
    """
    tmdb = TMDBClient()
    async def enriched(match: dict[str, Any] | None, matched_by: str = "title") -> dict[str, Any] | None:
        if not match:
            return None
        result = {**match, "matched_by": matched_by}
        resolved_imdb = imdb_id or result.get("imdb_id")
        if not resolved_imdb and result.get("id"):
            try:
                resolved_imdb = (await tmdb.movie_external_ids(int(result["id"]))).get("imdb_id")
            except Exception:
                resolved_imdb = None
        result["imdb_id"] = resolved_imdb
        return result
    if imdb_id:
        found = await tmdb.find_by_imdb(imdb_id)
        # IMDb 编号是权威标识：由 TMDB 按编号换算，年份相近即直接采用，不要求片名一致（导入片名可能是另一种语言）。
        trusted = [item for item in found if not year or is_year_close(_year(item), str(year), 1)]
        match = trusted[0] if trusted else select_tmdb_match(found, title, year)
        if match:
            return await enriched(match, "imdb")
    if tmdb_id:
        try:
            details = await tmdb.movie_details(int(tmdb_id))
        except httpx.HTTPStatusError as exc:
            # 来源里的编号已被 TMDB 删除或合并时，退回到片名识别。
            if exc.response.status_code != 404:
                raise
            details = {}
        if details.get("id"):
            return await enriched({**details, "imdb_id": imdb_id or details.get("imdb_id")}, "tmdb")
    options = await tmdb.search_movie(title, year)
    # 若超长复合片名未检索出结果，退避尝试主标题（截取逗号/冒号/分号/破折号前的主标题）
    if not options and any(sep in title for sep in (",", "：", ":", " - ")):
        short_title = re.split(r"[,:：]|\s+-\s+", title)[0].strip()
        if short_title and len(short_title) >= 2:
            options = await tmdb.search_movie(short_title, year)
    match = select_tmdb_match(options, title, year)
    if match:
        return await enriched(match)
    if not str(settings.tmdb_language or "").lower().startswith("en"):
        # 本地化搜索结果只带中文名与原名，英文片单里的国际译名（如 Cure → キュア）无从比对；
        # 再用英文搜索一次，命中后换回本地化字段，片单仍显示中文名。
        match = select_tmdb_match(await tmdb.search_movie(title, year, language="en-US"), title, year)
        if match:
            try:
                details = await tmdb.movie_details(int(match["id"]))
            except Exception:
                details = {}
            return await enriched({**match, **{key: details[key] for key in LOCALIZED_FIELDS if details.get(key)}})
    suggestion = await AIRecognitionClient().suggest(title, year)
    if not suggestion:
        return None
    options = await tmdb.search_movie(str(suggestion.get("original_title") or suggestion.get("title") or title), suggestion.get("year") or year)
    return await enriched(select_tmdb_match(
        options, str(suggestion.get("original_title") or suggestion.get("title") or title), suggestion.get("year") or year,
    ))


async def recognize_item(item: Any) -> dict[str, Any] | None:
    """按片单条目识别：先补齐来源身份（如 Letterboxd 影片页里的 TMDB / IMDb 编号），再调用 ``recognize_movie``。"""
    row = dict(item)
    source_tmdb_id = row.get("source_tmdb_id")
    imdb_id = row.get("imdb_id")
    source_ref = str(row.get("source_ref") or "")
    # Letterboxd 影片页同时给出 IMDb 与 TMDB 编号：缺任意一个就补取一次并缓存到片单条目。
    if (not source_tmdb_id or not imdb_id) and source_ref.startswith("letterboxd:"):
        from ..list_sources import PlaylistSourceFetcher

        try:
            found_tmdb, found_imdb = await PlaylistSourceFetcher().letterboxd_film_ids(source_ref.split(":", 1)[1])
        except Exception:
            # Letterboxd 暂时不可用不影响识别，退回到片名搜索，下次识别再补取。
            found_tmdb, found_imdb = None, None
        if found_tmdb or found_imdb:
            source_tmdb_id, imdb_id = source_tmdb_id or found_tmdb, imdb_id or found_imdb
            with connect() as conn:
                conn.execute(
                    "UPDATE playlist_items SET source_tmdb_id=COALESCE(source_tmdb_id,?),imdb_id=COALESCE(imdb_id,?) WHERE id=?",
                    (found_tmdb, found_imdb, row["id"]),
                )
    return await recognize_movie(str(row["original_title"]), row.get("year"), imdb_id, source_tmdb_id)


def tmdb_item_values(media: dict[str, Any], fallback_imdb: str | None = None) -> tuple[Any, ...]:
    release_year = str(media.get("release_date") or "")[:4]
    return (
        to_int(media["id"]), str(media.get("title") or "").strip() or None,
        str(media.get("original_title") or "").strip() or None,
        to_int(release_year) if release_year.isdigit() else None,
        media.get("imdb_id") or fallback_imdb, utc_now(),
    )


def tmdb_poster_path(media: dict[str, Any]) -> str | None:
    """Keep only TMDB's own image path shape; anything else is treated as unknown."""
    value = str(media.get("poster_path") or "").strip()
    return value if TMDB_POSTER_PATH.fullmatch(value) else None


def tmdb_original_language(media: dict[str, Any]) -> str | None:
    """TMDB 原语言（ISO 639-1，如 en、ja）；粤语片 TMDB 记为 cn，海报语言按中文处理。"""
    value = str(media.get("original_language") or "").strip().lower()
    if value == "cn":
        return "zh"
    return value if re.fullmatch(r"[a-z]{2}", value) else None


def persist_tmdb_item(item_id: int, media: dict[str, Any], fallback_imdb: str | None = None) -> None:
    values = tmdb_item_values(media, fallback_imdb)
    with connect() as conn:
        conn.execute(
            """UPDATE playlist_items
               SET fanart_poster_url=CASE WHEN tmdb_id IS ? THEN fanart_poster_url ELSE NULL END,
                   tmdb_alt_titles_json=CASE WHEN tmdb_id IS ? THEN tmdb_alt_titles_json ELSE NULL END,
                   fanart_backdrop_url=CASE WHEN tmdb_id IS ? THEN fanart_backdrop_url ELSE NULL END,
                   tmdb_backdrop_path=CASE WHEN tmdb_id IS ? THEN tmdb_backdrop_path ELSE NULL END,
                   tmdb_id=?,tmdb_title=?,tmdb_original_title=?,tmdb_year=?,tmdb_imdb_id=?,tmdb_checked_at=?,
                   tmdb_poster_path=COALESCE(?, tmdb_poster_path),
                   tmdb_original_language=COALESCE(?, tmdb_original_language)
               WHERE id=?""",
            (values[0], values[0], values[0], values[0], *values, tmdb_poster_path(media), tmdb_original_language(media), item_id),
        )



MAX_ALT_TITLES = 20


def usable_alt_titles(titles: list[str], known: list[Any]) -> list[str]:
    """只留能与种子标题比对的其他片名：含拉丁字母、数字或汉字，去掉与已有片名重复的，最多 20 个。"""
    seen = {normalized_title_text(value) for value in known if value}
    kept: list[str] = []
    for title in titles:
        key = normalized_title_text(title)
        if not meaningful_title(title) or key in seen:
            continue
        seen.add(key)
        kept.append(title)
        if len(kept) >= MAX_ALT_TITLES:
            break
    return kept


async def ensure_alt_titles(item: Any) -> Any:
    """寻片前补取 TMDB 其他片名并缓存；取不到不影响寻片，下次再试。返回最新的片单条目。"""
    row = dict(item)
    if not settings.tmdb_api_key or not row.get("tmdb_id") or row.get("tmdb_alt_titles_json") is not None:
        return item
    try:
        titles = await TMDBClient().movie_alternative_titles(to_int(row["tmdb_id"]))
    except Exception:
        return item
    known = [row.get(key) for key in ("tmdb_title", "tmdb_original_title", "original_title", "chinese_title")]
    with connect() as conn:
        conn.execute(
            "UPDATE playlist_items SET tmdb_alt_titles_json=? WHERE id=? AND tmdb_id IS ?",
            (json.dumps(usable_alt_titles(titles, known), ensure_ascii=False), row["id"], row["tmdb_id"]),
        )
        return conn.execute("SELECT * FROM playlist_items WHERE id=?", (row["id"],)).fetchone() or item
