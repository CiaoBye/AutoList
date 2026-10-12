"""Emby：媒体库、入库状态与海报。"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..config import settings
from ..outbound import safe_request


def quality_tier(width: Any, height: Any) -> int | None:
    """按画面宽度归到 2160 / 1080 / 720 / 480 档（宽银幕片的高度会被裁掉，不能只看高度）。"""
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return None
    if not w and not h:
        return None
    if w >= 3000 or h >= 2000:
        return 2160
    if w >= 1800 or h >= 1000:
        return 1080
    if w >= 1200 or h >= 700:
        return 720
    return 480


class EmbyClient:
    def _params(self) -> dict[str, str]:
        return {"api_key": settings.emby_api_key}

    async def check(self) -> dict[str, Any]:
        if not settings.emby_base_url or not settings.emby_api_key:
            return {"ok": False, "configured": False, "message": "未配置 Emby"}
        async with httpx.AsyncClient(base_url=settings.emby_base_url, timeout=settings.mp_timeout_seconds, follow_redirects=False) as client:
            response = await safe_request(
                client, "GET", "/emby/System/Info", params=self._params(), label="Emby 地址", allow_private=True,
            )
            response.raise_for_status()
            data = response.json()
        return {"ok": True, "configured": True, "server_name": data.get("ServerName"), "version": data.get("Version")}

    async def find_movie(
        self, title: str, year: int | None, tmdb_id: int | None = None, imdb_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not settings.emby_base_url or not settings.emby_api_key:
            return None
        base_params: dict[str, Any] = {
            **self._params(), "IncludeItemTypes": "Movie", "Recursive": "true",
            "Fields": "Path,ProviderIds,ProductionYear,OriginalTitle,ImageTags", "Limit": 50,
        }
        async with httpx.AsyncClient(base_url=settings.emby_base_url, timeout=settings.mp_timeout_seconds, follow_redirects=False) as client:
            exact_matches: list[dict[str, Any]] = []
            for provider, provider_id in (("tmdb", tmdb_id), ("imdb", imdb_id)):
                if not provider_id:
                    continue
                response = await safe_request(
                    client, "GET", "/emby/Items", params={**base_params, "AnyProviderIdEquals": f"{provider}.{provider_id}"},
                    label="Emby 地址", allow_private=True,
                )
                response.raise_for_status()
                exact_matches.extend(response.json().get("Items", []))
                if exact_matches:
                    break
            if exact_matches:
                # 同一影片可能同时存在实体文件与 .strm；实体文件才代表真正入库。
                return next(
                    (item for item in exact_matches if item.get("Path") and not str(item["Path"]).lower().endswith(".strm")),
                    exact_matches[0],
                )
            response = await safe_request(
                client, "GET", "/emby/Items", params={**base_params, "SearchTerm": title},
                label="Emby 地址", allow_private=True,
            )
            response.raise_for_status()
            items = response.json().get("Items", [])
        normalized_title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", title.lower())
        title_matches = []
        for item in items:
            names = (item.get("Name"), item.get("OriginalTitle"))
            normalized_names = {re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(name).lower()) for name in names if name}
            if normalized_title in normalized_names and (not year or str(item.get("ProductionYear")) == str(year)):
                title_matches.append(item)
        return next(
            (item for item in title_matches if item.get("Path") and not str(item["Path"]).lower().endswith(".strm")),
            title_matches[0] if title_matches else None,
        )

    async def library_match(
        self, title: str, year: int | None, tmdb_id: int | None = None, imdb_id: str | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        item = await self.find_movie(title, year, tmdb_id, imdb_id)
        if not item:
            return "not_found", None
        path = str(item.get("Path") or "")
        if not path:
            return "not_found", item
        return ("strm" if path.lower().endswith(".strm") else "in_library"), item

    async def versions(self, tmdb_id: int | None, imdb_id: str | None = None) -> list[dict[str, Any]]:
        """Emby 里同一部影片的全部实体文件版本（不含 .strm）：多个条目与合并条目内的多版本都算。"""
        if not settings.emby_base_url or not settings.emby_api_key:
            return []
        params: dict[str, Any] = {
            **self._params(), "IncludeItemTypes": "Movie", "Recursive": "true",
            "Fields": "Path,MediaSources", "Limit": 20,
        }
        items: list[dict[str, Any]] = []
        async with httpx.AsyncClient(base_url=settings.emby_base_url, timeout=settings.mp_timeout_seconds, follow_redirects=False) as client:
            for provider, provider_id in (("tmdb", tmdb_id), ("imdb", imdb_id)):
                if not provider_id:
                    continue
                response = await safe_request(
                    client, "GET", "/emby/Items", params={**params, "AnyProviderIdEquals": f"{provider}.{provider_id}"},
                    label="Emby 地址", allow_private=True,
                )
                response.raise_for_status()
                items = response.json().get("Items", [])
                if items:
                    break
        # 同一个文件可能同时出现在 .strm 占位条目与实体条目里（.strm 条目的来源只有路径、没有视频流）：
        # 按路径合并，保留信息最全的那一份。
        by_path: dict[str, dict[str, Any]] = {}
        for item in items:
            for source in item.get("MediaSources") or [{"Path": item.get("Path"), "Name": item.get("Name")}]:
                path = str(source.get("Path") or item.get("Path") or "")
                if not path or path.lower().endswith(".strm"):
                    continue
                video = next((stream for stream in source.get("MediaStreams") or [] if stream.get("Type") == "Video"), {})
                version = {
                    "name": path.rsplit("/", 1)[-1],
                    "size": source.get("Size") or None,
                    "resolution": f"{video['Width']}×{video['Height']}" if video.get("Width") and video.get("Height") else None,
                    "tier": quality_tier(video.get("Width"), video.get("Height")),
                }
                known = by_path.get(path)
                if known is None or (known["tier"] is None and version["tier"] is not None):
                    by_path[path] = version
        versions = list(by_path.values())
        return versions

    async def poster(self, item_id: str) -> tuple[bytes, str]:
        if not settings.emby_base_url or not settings.emby_api_key:
            raise RuntimeError("未配置 Emby")
        async with httpx.AsyncClient(base_url=settings.emby_base_url, timeout=settings.mp_timeout_seconds, follow_redirects=False) as client:
            response = await safe_request(
                client, "GET", f"/emby/Items/{item_id}/Images/Primary",
                params={**self._params(), "maxHeight": 720, "quality": 90}, label="Emby 地址", allow_private=True,
            )
            response.raise_for_status()
        return response.content, response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
