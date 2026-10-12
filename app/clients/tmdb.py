"""TMDB：识别、片名译名、海报与详情。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..config import public_endpoint_url, settings
from ..outbound import safe_request


TMDB_POSTER_MAX_BYTES = 4 * 1024 * 1024

class TMDBClient:
    base_url = "https://api.themoviedb.org/3"

    def _auth(self) -> tuple[dict[str, str], dict[str, str]]:
        key = settings.tmdb_api_key.strip()
        if not key:
            return {}, {}
        if key.startswith("eyJ"):
            return {"Authorization": f"Bearer {key}"}, {}
        return {}, {"api_key": key}

    async def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        headers, _ = self._auth()
        request_path = str(path or "").lstrip("/")
        if not request_path:
            raise ValueError("TMDB 请求路径不能为空")
        base_url = f"{self.base_url.rstrip('/')}/"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                proxy = settings.outbound_proxy_url if settings.tmdb_proxy_enabled and settings.outbound_proxy_url else None
                async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=settings.mp_timeout_seconds,
                                             proxy=proxy) as client:
                    response = await safe_request(
                        client, "GET", request_path, params=params, label="TMDB 地址",
                        proxy_mode=bool(proxy),
                    )
                    response.raise_for_status()
                    return response
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
            except httpx.HTTPStatusError as exc:
                # 429/5xx 属于临时性失败，退避重试；其他状态码直接上抛。
                if exc.response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    last_error = exc
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise
        proxy = public_endpoint_url(settings.outbound_proxy_url) if settings.tmdb_proxy_enabled and settings.outbound_proxy_url else "直连"
        endpoint = proxy or "已配置代理"
        raise RuntimeError(f"TMDB 网络连接失败（{endpoint}）：{type(last_error).__name__}") from last_error

    async def check(self) -> dict[str, Any]:
        if not settings.tmdb_api_key:
            return {"ok": False, "configured": False, "message": "未配置 TMDB API Key"}
        _, params = self._auth()
        await self._get("/configuration", params)
        return {"ok": True, "configured": True}

    async def search_movie(self, title: str, year: int | None = None, language: str | None = None) -> list[dict[str, Any]]:
        if not settings.tmdb_api_key:
            raise RuntimeError("请先在馆务中填写 TMDB API Key")
        _, auth_params = self._auth()
        params: dict[str, Any] = {"query": title, "language": language or settings.tmdb_language, **auth_params}
        if year:
            params["year"] = year
        response = await self._get("/search/movie", params)
        return response.json().get("results", [])

    async def find_by_imdb(self, imdb_id: str) -> list[dict[str, Any]]:
        if not settings.tmdb_api_key or not imdb_id:
            return []
        _, auth_params = self._auth()
        params = {"external_source": "imdb_id", "language": settings.tmdb_language, **auth_params}
        data = (await self._get(f"/find/{imdb_id}", params)).json()
        return data.get("movie_results", [])

    async def movie_external_ids(self, tmdb_id: int) -> dict[str, Any]:
        _, params = self._auth()
        return (await self._get(f"/movie/{tmdb_id}/external_ids", params)).json()

    async def movie_translations(self, tmdb_id: int) -> list[dict[str, Any]]:
        """TMDB 各语言 / 地区的译名与简介（zh-CN 为空时，新加坡、香港、台湾常有中文译名）。"""
        _, params = self._auth()
        data = (await self._get(f"/movie/{int(tmdb_id)}/translations", params)).json()
        return data.get("translations", []) if isinstance(data, dict) else []

    async def movie_alternative_titles(self, tmdb_id: int) -> list[str]:
        """TMDB 记录的其他片名（各地区译名、罗马拼音等），站点种子常用其中某个。"""
        _, params = self._auth()
        data = (await self._get(f"/movie/{int(tmdb_id)}/alternative_titles", params)).json()
        return [str(item.get("title") or "").strip() for item in data.get("titles", []) if str(item.get("title") or "").strip()]

    async def movie_details(self, tmdb_id: int) -> dict[str, Any]:
        if not settings.tmdb_api_key:
            raise RuntimeError("请先在馆务中填写 TMDB API Key")
        _, params = self._auth()
        return (await self._get(f"/movie/{int(tmdb_id)}", {"language": settings.tmdb_language, **params})).json()

    async def tv_details(self, tmdb_id: int) -> dict[str, Any]:
        if not settings.tmdb_api_key:
            raise RuntimeError("请先在馆务中填写 TMDB API Key")
        _, params = self._auth()
        return (await self._get(f"/tv/{int(tmdb_id)}", {"language": settings.tmdb_language, **params})).json()

    async def poster_image(self, poster_path: str, size: str = "w342") -> tuple[bytes, str]:
        """Download one TMDB poster; the caller validates ``poster_path`` against TMDB's path shape."""
        proxy = settings.outbound_proxy_url if settings.tmdb_proxy_enabled and settings.outbound_proxy_url else None
        async with httpx.AsyncClient(timeout=settings.mp_timeout_seconds, follow_redirects=False, proxy=proxy) as client:
            response = await safe_request(
                client, "GET", f"https://image.tmdb.org/t/p/{size}{poster_path}", label="TMDB 海报",
                proxy_mode=bool(proxy), max_response_bytes=TMDB_POSTER_MAX_BYTES,
            )
            response.raise_for_status()
        return response.content, response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
