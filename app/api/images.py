"""海报与剧照代理：fanart.tv → Emby → TMDB，不向浏览器暴露任何 API Key。"""

from __future__ import annotations


import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..clients import FANART_POSTER_URL, FanartClient, TMDBClient
from ..config import settings
from ..database import connect
from ..logs import event_logger
from ..queries.films import film_artwork, remember_artwork
from ..security import safe_error
from ..services.films import (
    fanart_version,
)
from ..services.recognition import TMDB_POSTER_PATH
from ..state import poster_cache, remember_poster
from ..util import raster_image_media_type, to_int

router = APIRouter()


@router.get("/api/playlist-items/{item_id}/tmdb-poster")
async def playlist_item_tmdb_poster(item_id: int) -> Response:
    """Proxy a TMDB poster so films outside Emby still get artwork without exposing the TMDB key."""
    with connect() as conn:
        row = film_artwork(conn, item_id)
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
            remember_artwork(conn, item_id, to_int(row["tmdb_id"]), "tmdb_poster_path", poster_path)
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
            remember_artwork(conn, item_id, tmdb_id, "tmdb_original_language", language)
    return language


@router.get("/api/playlist-items/{item_id}/backdrop")
async def playlist_item_backdrop(item_id: int, v: str = "") -> Response:
    """影片详情横幅：fanart.tv 剧照优先，没有时用 TMDB 剧照（w1280）。"""
    with connect() as conn:
        row = film_artwork(conn, item_id)
    if not row or not row["tmdb_id"]:
        raise HTTPException(404, "影片尚未识别")
    tmdb_id = to_int(row["tmdb_id"])
    fanart_url, tmdb_path = row["fanart_backdrop_url"], row["tmdb_backdrop_path"]

    def remember(column: str, value: str) -> None:
        with connect() as conn:
            remember_artwork(conn, item_id, tmdb_id, column, value)

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
        row = film_artwork(conn, item_id)
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
                remember_artwork(conn, item_id, to_int(row["tmdb_id"]), "fanart_poster_url", poster_url)
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
