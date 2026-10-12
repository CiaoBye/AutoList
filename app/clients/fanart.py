"""fanart.tv：海报与剧照。"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..config import settings
from ..outbound import safe_request
from ..util import to_int


FANART_POSTER_MAX_BYTES = 8 * 1024 * 1024
# 只接受 fanart.tv 自己的图片资源地址，防止把任意 URL 写进数据库后由服务端代为请求。
# 当前接口返回 /fanart/<名称>.jpg，早期为 /fanart/movies/<编号>/movieposter/<名称>.jpg，两种都接受。
FANART_POSTER_URL = re.compile(
    r"https://assets\.fanart\.tv/fanart/(?:movies/\d+/(?:movieposter|moviebackground)/)?[A-Za-z0-9._-]+\.(?:jpg|jpeg|png)"
)


def fanart_poster_rank(poster: dict[str, Any], original_language: str | None) -> tuple[int, int]:
    """影片原语言的海报优先（英语片要英文版、日语片要日文版），其次无字版、英文版；同语言取点赞最多。"""
    lang = str(poster.get("lang") or "").lower()
    order = {"00": 1, "": 1, "en": 2}
    if original_language:
        order[original_language.lower()] = 0
    return order.get(lang, 3), -to_int(poster.get("likes"))


class FanartClient:
    base_url = "https://webservice.fanart.tv/v3"

    def _proxy(self) -> str | None:
        # fanart.tv 与 TMDB 同属影片元数据源，共用“TMDB 走代理”开关。
        return settings.outbound_proxy_url if settings.tmdb_proxy_enabled and settings.outbound_proxy_url else None

    async def _movie(self, movie_id: int | str) -> httpx.Response:
        proxy = self._proxy()
        async with httpx.AsyncClient(timeout=settings.mp_timeout_seconds, follow_redirects=False, proxy=proxy) as client:
            return await safe_request(
                client, "GET", f"{self.base_url}/movies/{movie_id}",
                headers={"api-key": settings.fanart_api_key.strip()}, label="Fanart 地址", proxy_mode=bool(proxy),
            )

    async def check(self) -> dict[str, Any]:
        if not settings.fanart_api_key:
            return {"ok": False, "configured": False, "message": "未配置 Fanart API Key"}
        response = await self._movie(550)
        if response.status_code in {401, 403}:
            return {"ok": False, "configured": True, "message": "Fanart API Key 无效"}
        response.raise_for_status()
        return {"ok": True, "configured": True}

    async def movie_poster_url(self, tmdb_id: int, original_language: str | None = None) -> str:
        """Return the best poster URL for a TMDB movie, or ``""`` when fanart.tv has none."""
        if not settings.fanart_api_key:
            raise RuntimeError("请先在馆务中填写 Fanart API Key")
        response = await self._movie(int(tmdb_id))
        if response.status_code == 404:
            return ""
        response.raise_for_status()
        posters = []
        for poster in response.json().get("movieposter") or []:
            if not isinstance(poster, dict):
                continue
            url = re.sub(r"^http://", "https://", str(poster.get("url") or "").strip())
            if FANART_POSTER_URL.fullmatch(url):
                posters.append({**poster, "url": url})
        if not posters:
            return ""
        return str(min(posters, key=lambda poster: fanart_poster_rank(poster, original_language))["url"])

    async def movie_background_url(self, tmdb_id: int) -> str:
        """横幅剧照：无字版优先，其次点赞最多；fanart.tv 没有时返回 ``""``。"""
        if not settings.fanart_api_key:
            raise RuntimeError("请先在馆务中填写 Fanart API Key")
        response = await self._movie(int(tmdb_id))
        if response.status_code == 404:
            return ""
        response.raise_for_status()
        backgrounds = []
        for item in response.json().get("moviebackground") or []:
            url = re.sub(r"^http://", "https://", str(item.get("url") or "").strip()) if isinstance(item, dict) else ""
            if FANART_POSTER_URL.fullmatch(url):
                backgrounds.append({**item, "url": url})
        if not backgrounds:
            return ""
        textless = lambda item: str(item.get("lang") or "").lower() in {"", "00"}
        return str(min(backgrounds, key=lambda item: (not textless(item), -to_int(item.get("likes"))))["url"])

    async def background_image(self, url: str) -> tuple[bytes, str]:
        """剧照原图（1920 宽），抽屉横幅在高分屏上需要足够的分辨率。"""
        proxy = self._proxy()
        async with httpx.AsyncClient(timeout=settings.mp_timeout_seconds, follow_redirects=False, proxy=proxy) as client:
            response = await safe_request(
                client, "GET", url, label="Fanart 剧照", proxy_mode=bool(proxy), max_response_bytes=FANART_POSTER_MAX_BYTES,
            )
            response.raise_for_status()
        return response.content, response.headers.get("content-type", "image/jpeg").split(";", 1)[0]

    async def poster_image(self, poster_url: str) -> tuple[bytes, str]:
        """Download a poster, preferring fanart.tv's 400px preview and falling back to the original."""
        proxy = self._proxy()
        async with httpx.AsyncClient(timeout=settings.mp_timeout_seconds, follow_redirects=False, proxy=proxy) as client:
            response = None
            for url in (poster_url.replace("/fanart/", "/bigpreview/", 1), poster_url):
                response = await safe_request(
                    client, "GET", url, label="Fanart 海报", proxy_mode=bool(proxy), max_response_bytes=FANART_POSTER_MAX_BYTES,
                )
                if response.status_code != 404:
                    break
            assert response is not None
            response.raise_for_status()
        return response.content, response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
