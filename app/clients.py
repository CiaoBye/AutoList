import base64
import asyncio
import html
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from defusedxml import ElementTree

from .config import APP_VERSION, public_endpoint_url, settings
from .parsers import AccountTableParser, human_size_bytes, numeric_value, site_proxy
from .util import to_float, to_int
from .outbound import safe_request



# 按 (timeout, proxy) 缓存模块级 AsyncClient，供搜索/检测方法复用连接池（审计 3-24）。
_search_clients: dict[tuple[float, str | None], httpx.AsyncClient] = {}


def _search_client(timeout: float, proxy: str | None = None) -> httpx.AsyncClient:
    key = (float(timeout), proxy)
    client = _search_clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout, proxy=proxy, follow_redirects=False)
        _search_clients[key] = client
    return client


async def close_search_clients() -> None:
    """Close the shared PT/search connection pools during shutdown and tests.

    The cache is intentionally process-local so search requests can reuse TCP
    connections.  Clearing only the dictionary leaves the underlying sockets
    open until garbage collection, which is especially visible as
    ``ResourceWarning`` on Python 3.14 and can retain proxy connections after
    a reload.  Remove the references first, then close each client defensively
    so one broken pool cannot prevent the remaining pools from being released.
    """
    clients = list(_search_clients.values())
    _search_clients.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception:
            continue


class MoviePilotClient:
    """MoviePilot gateway kept only for download classification and organization."""

    def __init__(self) -> None:
        self.base_url = settings.mp_base_url
        self.headers = {"X-API-KEY": settings.mp_api_key} if settings.mp_api_key else {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=settings.mp_timeout_seconds, follow_redirects=False)

    async def check(self) -> dict[str, Any]:
        if not self.base_url or not settings.mp_api_key:
            return {"ok": False, "configured": False, "message": "未配置 MoviePilot"}
        async with self._client() as client:
            response = await safe_request(
                client, "GET", "/api/v1/download/clients", headers=self.headers, label="MoviePilot 地址", allow_private=True,
            )
            response.raise_for_status()
            return {"ok": True, "configured": True, "downloaders": response.json()}

    async def download(self, media_in: dict[str, Any], torrent_in: dict[str, Any], downloader: str | None = None) -> Any:
        torrent_payload = dict(torrent_in)
        factor = torrent_payload.pop("volume_factor", None)
        if factor is not None and torrent_payload.get("downloadvolumefactor") is None:
            torrent_payload["downloadvolumefactor"] = to_float(factor)
        publish_time = torrent_payload.pop("publish_time", None)
        if publish_time and not torrent_payload.get("pubdate"):
            torrent_payload["pubdate"] = str(publish_time)
        torrent_payload.pop("detail_url", None)
        async with self._client() as client:
            response = await safe_request(
                client, "POST", "/api/v1/download/", headers=self.headers,
                json={"media_in": media_in, "torrent_in": torrent_payload, "downloader": downloader},
                label="MoviePilot 地址", allow_private=True,
            )
            response.raise_for_status()
            return response.json()

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
            raise RuntimeError("请先在设置中填写 TMDB API Key")
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

    async def movie_alternative_titles(self, tmdb_id: int) -> list[str]:
        """TMDB 记录的其他片名（各地区译名、罗马拼音等），站点种子常用其中某个。"""
        _, params = self._auth()
        data = (await self._get(f"/movie/{int(tmdb_id)}/alternative_titles", params)).json()
        return [str(item.get("title") or "").strip() for item in data.get("titles", []) if str(item.get("title") or "").strip()]

    async def movie_details(self, tmdb_id: int) -> dict[str, Any]:
        if not settings.tmdb_api_key:
            raise RuntimeError("请先在设置中填写 TMDB API Key")
        _, params = self._auth()
        return (await self._get(f"/movie/{int(tmdb_id)}", {"language": settings.tmdb_language, **params})).json()

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
            raise RuntimeError("请先在设置中填写 Fanart API Key")
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
            raise RuntimeError("请先在设置中填写 Fanart API Key")
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


class AIRecognitionClient:
    async def suggest(self, title: str, year: int | None) -> dict[str, Any] | None:
        if not settings.ai_base_url or not settings.ai_api_key or not settings.ai_model:
            return None
        prompt = (
            "识别下面的电影名称，只返回JSON对象，字段为 title、original_title、year。"
            "不要解释，不确定时保持输入。\n"
            f"输入标题：{title}\n输入年份：{year or ''}"
        )
        headers = {"Authorization": f"Bearer {settings.ai_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": settings.ai_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(base_url=settings.ai_base_url, headers=headers, timeout=settings.mp_timeout_seconds) as client:
            response = await safe_request(
                client, "POST", "chat/completions", headers=headers, json=payload, label="AI 地址", allow_private=True,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.S)
        return json.loads(match.group(0)) if match else None


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


_global_transmission_session_id: str = ""


class TransmissionClient:
    def __init__(self) -> None:
        self.base_url = settings.tr_base_url.rstrip("/")
        if self.base_url and not self.base_url.endswith("/transmission/rpc"):
            self.base_url += "/transmission/rpc"
        self.auth = (
            httpx.BasicAuth(settings.tr_username, settings.tr_password or "")
            if settings.tr_username or settings.tr_password
            else None
        )
        self.session_id = _global_transmission_session_id

    async def _rpc(self, method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        global _global_transmission_session_id
        if not self.base_url:
            raise RuntimeError("未配置 Transmission 地址")
        session_id = _global_transmission_session_id or self.session_id
        headers = {"X-Transmission-Session-Id": session_id} if session_id else {}
        async with httpx.AsyncClient(timeout=settings.mp_timeout_seconds, follow_redirects=False) as client:
            response = await safe_request(
                client, "POST", self.base_url, headers=headers, json={"method": method, "arguments": arguments or {}},
                auth=self.auth, label="Transmission 地址", allow_private=True,
            )
            if response.status_code == 409:
                _global_transmission_session_id = response.headers.get("X-Transmission-Session-Id", "")
                self.session_id = _global_transmission_session_id
                response = await safe_request(
                    client, "POST", self.base_url, headers={"X-Transmission-Session-Id": _global_transmission_session_id},
                    json={"method": method, "arguments": arguments or {}}, auth=self.auth, label="Transmission 地址",
                    allow_private=True,
                )
            response.raise_for_status()
            data = response.json()
        if data.get("result") != "success":
            raise RuntimeError(data.get("result") or "Transmission RPC 失败")
        return data.get("arguments", {})

    async def check(self) -> dict[str, Any]:
        if not self.base_url:
            return {"ok": False, "configured": False, "message": "未配置 Transmission 地址"}
        data = await self._rpc("session-get")
        return {"ok": True, "configured": True, "version": data.get("version"), "rpc_version": data.get("rpc-version")}

    async def current_downloads(self) -> list[dict[str, Any]]:
        if not self.base_url:
            return []
        fields = ["id", "name", "hashString", "status", "percentDone", "totalSize", "labels", "downloadDir"]
        data = await self._rpc("torrent-get", {"fields": fields})
        return data.get("torrents", [])


class TorznabClient:
    """Independent PT adapter for Torznab-compatible endpoints (Prowlarr/Jackett/custom gateways)."""

    async def search(self, site: dict[str, Any], title: str, imdb_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"t": "movie", "q": title, "apikey": site.get("api_key", "")}
        if imdb_id:
            params["imdbid"] = imdb_id.removeprefix("tt")
        proxy = site_proxy(site)
        client = _search_client(settings.mp_timeout_seconds, proxy)
        response = await safe_request(
            client, "GET", site["base_url"], params=params, label=f"站点 {site.get('name', '')} 地址",
            proxy_mode=bool(proxy),
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        results: list[dict[str, Any]] = []
        for item in root.findall(".//item"):
            attrs = {node.attrib.get("name"): node.attrib.get("value") for node in item if node.tag.endswith("attr")}
            enclosure = item.find("enclosure")
            try:
                size = to_int(attrs.get("size") or item.findtext("size") or 0)
            except (TypeError, ValueError):
                size = 0
            try:
                seeders = to_int(attrs.get("seeders") or 0)
            except (TypeError, ValueError):
                seeders = 0
            results.append({
                "title": item.findtext("title") or "未知资源",
                "site_name": site["name"],
                "enclosure": enclosure.attrib.get("url") if enclosure is not None else item.findtext("link"),
                "size": size,
                "seeders": seeders,
                "publish_time": item.findtext("pubDate"),
                "detail_url": item.findtext("link"),
                "labels": [],
            })
        return results


class RSSClient:
    """Independent RSS/Atom adapter; private feed URLs remain server-side only."""

    async def search(self, site: dict[str, Any], title: str, imdb_id: str | None = None) -> list[dict[str, Any]]:
        feed_url = str(site.get("rss_url") or "").strip()
        if not feed_url:
            raise RuntimeError("站点未配置 RSS 地址")
        headers = {"User-Agent": str(site.get("user_agent") or "AutoList")}
        if site.get("cookie"):
            headers["Cookie"] = str(site["cookie"])
        proxy = site_proxy(site)
        async with httpx.AsyncClient(
            timeout=to_int(site.get("timeout_seconds") or 30), follow_redirects=False, proxy=proxy,
        ) as client:
            response = await safe_request(
                client, "GET", feed_url, headers=headers, label=f"站点 {site.get('name', '')} RSS 地址",
                proxy_mode=bool(proxy),
            )
            response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        normalized_title = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        title_tokens = {token for token in normalized_title.split() if len(token) > 2}
        results: list[dict[str, Any]] = []
        for item in root.findall(".//item") + root.findall(".//{*}entry"):
            torrent_title = (item.findtext("title") or item.findtext("{*}title") or "").strip()
            haystack = torrent_title.lower()
            if imdb_id and imdb_id.lower() not in haystack and title_tokens and not title_tokens.issubset(set(re.sub(r"[^a-z0-9]+", " ", haystack).split())):
                continue
            if not imdb_id and title_tokens and not title_tokens.issubset(set(re.sub(r"[^a-z0-9]+", " ", haystack).split())):
                continue
            enclosure = item.find("enclosure")
            if enclosure is None:
                enclosure = item.find("{*}enclosure")
            link = enclosure.attrib.get("url") if enclosure is not None else item.findtext("link")
            if not link:
                atom_link = item.find("{*}link")
                link = atom_link.attrib.get("href") if atom_link is not None else None
            if not link:
                continue
            try:
                size = to_int((enclosure.attrib.get("length") if enclosure is not None else "0") or 0)
            except (TypeError, ValueError):
                size = 0
            results.append({
                "title": torrent_title or "未知资源", "site_name": site["name"], "enclosure": link,
                "size": size, "seeders": 0,
                "publish_time": item.findtext("pubDate") or item.findtext("{*}published") or item.findtext("{*}updated"),
                "labels": [], "site_cookie": site.get("cookie") or "", "site_ua": headers["User-Agent"],
            })
        return results

    async def check(self, site: dict[str, Any]) -> dict[str, Any]:
        results = await self.search(site, "AutoListConnectionProbe")
        return {"ok": True, "message": f"RSS 可用，当前匹配 {len(results)} 条探测结果"}


class MTeamClient:
    """M-Team API adapter aligned with PT Depiler's mTorrent implementation."""

    @staticmethod
    def api_base(site: dict[str, Any]) -> str:
        parsed = urlparse(str(site.get("base_url") or ""))
        parts = parsed.hostname.split(".") if parsed.hostname else []
        if parts:
            parts[0] = "api"
        host = ".".join(parts)
        if parsed.port:
            host += f":{parsed.port}"
        return urlunparse((parsed.scheme or "https", host, "", "", "", "")).rstrip("/")

    @staticmethod
    def headers(site: dict[str, Any]) -> dict[str, str]:
        return {
            "x-api-key": str(site.get("api_key") or ""),
            "Origin": str(site.get("base_url") or "").rstrip("/"),
            "User-Agent": str(site.get("user_agent") or f"AutoList/{APP_VERSION}"),
        }

    @staticmethod
    def _require_success(body: dict[str, Any], action: str) -> None:
        """Reject ambiguous M-Team responses instead of treating missing codes as success."""
        code = body.get("code")
        message = str(body.get("message") or "").strip()
        if code is not None:
            success = str(code) == "0"
        else:
            success = message.upper() == "SUCCESS"
        if not success:
            raise RuntimeError(message or f"M-Team {action}失败")

    @staticmethod
    def _discount_parts(discount: str) -> tuple[float, bool]:
        """Split an M-Team discount such as ``_2X_FREE`` into (download factor, double upload)."""
        value = str(discount or "").strip().upper()
        double_upload = value.startswith("_2X")
        value = value.removeprefix("_2X").lstrip("_")
        if value == "FREE":
            return 0.0, double_upload
        match = re.fullmatch(r"PERCENT_(\d+)", value)
        if match:
            return min(0.95, max(0.05, to_int(match.group(1)) / 100)), double_upload
        return 1.0, double_upload

    @classmethod
    def _discount_factor(cls, discount: str) -> float:
        """Map M-Team promotion discounts to a volume factor (0=free, 1=full price)."""
        return cls._discount_parts(discount)[0]

    def _parse_row(self, row: dict[str, Any], site: dict[str, Any]) -> dict[str, Any]:
        status = row.get("status") or {}
        discount = (status.get("promotionRule") or {}).get("discount") or ("FREE" if status.get("mallSingleFree") else status.get("discount")) or "NORMAL"
        factor, double_upload = self._discount_parts(discount)
        labels = list(row.get("labelsNew") or [])
        if factor == 0:
            labels.insert(0, "FREE")
        elif factor < 1:
            labels.insert(0, f"{to_int(factor * 100)}%")
        if double_upload:
            labels.insert(0, "2X")
        request_options = {
            "method": "post", "cookie": False, "params": {"id": str(row.get("id"))},
            "header": {**self.headers(site), "Content-Type": "multipart/form-data"}, "result": "data",
        }
        encoded = base64.b64encode(json.dumps(request_options).encode()).decode()
        publish_time = next((row.get(key) for key in ("createTime", "addedAt", "publishTime", "createdTime", "pubDate") if row.get(key) not in (None, "")), None)
        detail_url = next((row.get(key) for key in ("detailUrl", "torrentUrl", "url", "link") if row.get(key) not in (None, "")), None)
        if not detail_url and row.get("id") is not None:
            detail_url = f"{self.api_base(site)}/details.php?id={row.get('id')}"
        return {
            "title": row.get("name") or "未知资源", "description": row.get("smallDescr"),
            "site_name": site["name"], "size": to_int(row.get("size") or 0),
            "seeders": to_int(status.get("seeders") or 0), "leechers": to_int(status.get("leechers") or 0),
            "enclosure": f"[{encoded}]{self.api_base(site)}/api/torrent/genDlToken",
            "labels": labels, "volume_factor": factor, "site_ua": site.get("user_agent") or f"AutoList/{APP_VERSION}",
            "publish_time": publish_time, "detail_url": detail_url,
        }

    async def search(self, site: dict[str, Any], title: str, imdb_id: str | None = None) -> list[dict[str, Any]]:
        keyword = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else title
        timeout = to_int(site.get("timeout_seconds") or 30)
        rows: list[dict[str, Any]] = []
        proxy = site_proxy(site)
        client = _search_client(timeout=timeout, proxy=proxy)
        # 分页拉取（最多 5 页 = 500 条），避免热门影片被硬截断到 100 条。
        for page in range(1, 6):
            payload = {"pageNumber": page, "pageSize": 100, "mode": "normal", "keyword": keyword}
            response = await safe_request(
                client, "POST", f"{self.api_base(site)}/api/torrent/search", headers=self.headers(site),
                json=payload, label=f"站点 {site.get('name', '')} API 地址", proxy_mode=bool(proxy),
            )
            response.raise_for_status()
            body = response.json()
            self._require_success(body, "API 搜索")
            data = body.get("data") or {}
            page_rows = data.get("data") or []
            rows.extend(page_rows)
            total_pages = to_int(data.get("totalPages") or 1)
            if len(page_rows) < 100 or page >= total_pages:
                break
        return [self._parse_row(row, site) for row in rows]

    async def check(self, site: dict[str, Any]) -> dict[str, Any]:
        results = await self.search(site, "AutoListConnectionProbe")
        return {"ok": True, "message": f"API 可用，探测返回 {len(results)} 条"}

    async def account_stats(self, site: dict[str, Any]) -> dict[str, Any]:
        timeout = to_int(site.get("timeout_seconds") or 30)
        proxy = site_proxy(site)
        client = _search_client(timeout=timeout, proxy=proxy)
        response = await safe_request(
            client, "POST", f"{self.api_base(site)}/api/member/profile", headers=self.headers(site),
            json={}, label=f"站点 {site.get('name', '')} API 地址", proxy_mode=bool(proxy),
        )
        response.raise_for_status()
        body = response.json()
        self._require_success(body, "账户统计请求")
        source = body.get("data") or {}

        def find(keys: set[str], value: Any = source) -> Any:
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).casefold() in keys and child not in (None, ""):
                        return child
                for child in value.values():
                    found = find(keys, child)
                    if found not in (None, ""):
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = find(keys, child)
                    if found not in (None, ""):
                        return found
            return None

        uploaded = human_size_bytes(find({"uploaded", "upload", "uploadamount"}))
        downloaded = human_size_bytes(find({"downloaded", "download", "downloadamount"}))
        if uploaded is None or downloaded is None:
            raise RuntimeError("M-Team 返回数据中未找到上传量或下载量")
        return {
            "uploaded": uploaded,
            "downloaded": downloaded,
            "ratio": numeric_value(find({"ratio", "sharerate", "share_ratio"})),
            "bonus": numeric_value(find({"bonus", "bonuspoints", "bonus_point"})),
            "seeding": to_int(numeric_value(find({"seeding", "seedcount", "seed_count"})) or 0),
        }


# Some sites use different search page endpoints (module-level constant, never mutated).
class NexusPHPClient:
    """Cookie 登录的 PT 站点：搜索与检测交给 ``app.sites`` 按站点档案处理，这里保留账户统计。"""

    # Some sites (e.g. hdarea.club) aggressively rate-limit automated User-Agents.
    _BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    async def search(self, site: dict[str, Any], title: str, imdb_id: str | None = None) -> list[dict[str, Any]]:
        from .sites import search

        return await search(site, title, imdb_id)

    async def check(self, site: dict[str, Any]) -> dict[str, Any]:
        from .sites import check

        return await check(site)

    @staticmethod
    def _banner_stats(text: str) -> dict[str, Any] | None:
        """解析传统 NexusPHP 首页欢迎横幅（上传量/下载量/分享率/魔力值/当前活动）。
        横幅是 NexusPHP 标准模板的已登录统计块，比用户详情页表格更通用。"""
        home_text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        home_text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", home_text)))
        # 任意统计标签或横幅常见词都作为值边界，避免值吞掉后续内容。
        stop_labels = (
            "上传量", "上傳量", "uploaded", "下载量", "下載量", "downloaded",
            "分享率", "比率", "ratio", "魔力值", "魔力豆", "魔力", "积分", "bonus",
            "当前活动", "當前活動", "做种数", "做種數", "seeding", "邀请", "邀請",
            "捐赠", "捐贈", "用户组", "用戶組", "可连接", "可連接", "连接数", "連接數",
            "上传排行", "上傳排行",
        )
        stop_pattern = "|".join(re.escape(label) for label in stop_labels)

        def banner_value(labels: tuple[str, ...]) -> str | None:
            pattern = "|".join(re.escape(label) for label in labels)
            match = re.search(
                rf"(?:{pattern})(?=[\s:：\[])\s*[:：]?\s*"
                rf"([^\s][^<]{{0,60}}?)(?=\s*(?:{stop_pattern})\s*[:：\[]?|\s*$)",
                home_text, re.I,
            )
            return match.group(1).strip() if match else None

        uploaded = human_size_bytes(banner_value(("上传量", "上傳量", "uploaded")))
        downloaded = human_size_bytes(banner_value(("下载量", "下載量", "downloaded")))
        if uploaded is None or downloaded is None:
            return None
        return {
            "uploaded": uploaded,
            "downloaded": downloaded,
            "ratio": numeric_value(banner_value(("分享率", "比率", "ratio"))),
            "bonus": numeric_value(banner_value(("魔力值", "魔力豆", "魔力", "积分", "bonus"))),
            "seeding": to_int(numeric_value(banner_value(("当前活动", "當前活動", "做种数", "做種數", "seeding"))) or 0),
        }

    @staticmethod
    def _tnode_stats(data: dict[str, Any]) -> dict[str, Any] | None:
        """TNode SPA 站点（如朱雀）的 /api/user/getInfo 用户信息。"""
        uploaded = to_int(data.get("upload") or 0)
        downloaded = to_int(data.get("download") or 0)
        if not uploaded and not downloaded:
            return None
        return {
            "uploaded": uploaded,
            "downloaded": downloaded,
            "ratio": (uploaded / downloaded) if downloaded > 0 else None,
            "bonus": numeric_value(data.get("bonus")),
            "seeding": to_int(data.get("seeding") or 0),
        }

    async def account_stats(self, site: dict[str, Any]) -> dict[str, Any]:
        base = str(site.get("base_url") or "").rstrip("/") + "/"
        headers = {
            "Cookie": str(site.get("cookie") or ""),
            "User-Agent": str(site.get("user_agent") or self._BROWSER_UA),
        }
        timeout = to_int(site.get("timeout_seconds") or 30)
        proxy = site_proxy(site)
        client = _search_client(timeout=timeout, proxy=proxy)
        home = await safe_request(
            client, "GET", base, headers=headers, label=f"站点 {site.get('name', '')} 地址",
            proxy_mode=bool(proxy),
        )
        home.raise_for_status()
        banner = self._banner_stats(home.text)
        if banner:
            return banner
        # TNode SPA（朱雀等）：首页带 x-csrf-token，账户信息走 JSON API。
        csrf = re.search(r'<meta name="x-csrf-token" content="([^"]+)"', home.text)
        if csrf:
            info = await safe_request(
                client, "GET", urljoin(base, "api/user/getInfo"),
                headers={
                    **headers, "x-csrf-token": csrf.group(1),
                    "X-Requested-With": "XMLHttpRequest", "Referer": base,
                }, label=f"站点 {site.get('name', '')} 地址", proxy_mode=bool(proxy),
            )
            info.raise_for_status()
            try:
                data = (info.json().get("data") or {})
            except ValueError:
                data = {}
            tnode = self._tnode_stats(data)
            if tnode:
                return tnode
        user_link = re.search(
            r"""href=["']([^"']*userdetails\.php\?[^"']*\bid=\d+[^"']*)["']""",
            home.text,
            re.I,
        )
        details_url = urljoin(base, html.unescape(user_link.group(1))) if user_link else urljoin(base, "userdetails.php")
        details = await safe_request(
            client, "GET", details_url, headers=headers, label=f"站点 {site.get('name', '')} 地址",
            proxy_mode=bool(proxy),
        )
        details.raise_for_status()
        parser = AccountTableParser()
        parser.feed(details.text)
        values: dict[str, str] = {}
        for row in parser.rows:
            for index, cell in enumerate(row[:-1]):
                label = re.sub(r"[\s:：]+", "", cell).casefold()
                if label:
                    values[label] = row[index + 1]
        # 详情页正文同样先剥离 script/style，避免 JS 模板字符串里的标签干扰匹配。
        page_text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", details.text)
        page_text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page_text)))

        def match_value(labels: tuple[str, ...]) -> str | None:
            table_value = next((value for label, value in values.items() if any(key in label for key in labels)), None)
            if table_value:
                return table_value
            pattern = "|".join(re.escape(label) for label in labels)
            # 正向：标签[:：]值（标准 NexusPHP 详情页；值必须为数字[+单位]，避免截断）
            match = re.search(
                rf"(?:{pattern})\s*[:：]\s*([\d][\d.,]*(?:\s*[TGMK]i?B)?)(?=\s|$)",
                page_text, re.I,
            )
            if match:
                return match.group(1)
            # 反向：值 标签（如观众站“111.555 TB 上传量”，标签在数值之后、无冒号）
            match = re.search(rf"([\d][\d.,]*(?:\s*[TGMK]i?B)?)\s+(?:{pattern})(?=\s|$)", page_text, re.I)
            if match:
                return match.group(1)
            # 空格分隔正向：标签 值（无冒号）
            match = re.search(rf"(?:{pattern})\s+([\d][\d.,]*(?:\s*[TGMK]i?B)?)(?=\s|$)", page_text, re.I)
            return match.group(1) if match else None

        uploaded = human_size_bytes(match_value(("上传量", "上傳量", "uploaded")))
        downloaded = human_size_bytes(match_value(("下载量", "下載量", "downloaded")))
        if uploaded is None or downloaded is None:
            raise RuntimeError("站点账户页未找到上传量或下载量，请检查 Cookie 与 User-Agent")
        return {
            "uploaded": uploaded,
            "downloaded": downloaded,
            "ratio": numeric_value(match_value(("分享率", "比率", "ratio"))),
            "bonus": numeric_value(match_value(("魔力值", "魔力", "积分", "bonus"))),
            "seeding": to_int(numeric_value(match_value(("做种数", "seeding", "seedcount"))) or 0),
        }

    @staticmethod
    def _looks_like_login_page(response: httpx.Response) -> bool:
        """Detect a successful HTTP response that is actually an expired-cookie login page."""
        path = (response.url.path or "").lower()
        if path.endswith(("/login.php", "/takelogin.php")):
            return True
        text = response.text[:200_000]
        has_password = bool(re.search(r"<input[^>]+(?:name|type)=[\"'](?:password|passwd)[\"']", text, re.I))
        has_username = bool(re.search(r"<input[^>]+name=[\"'](?:username|user|uid)[\"']", text, re.I))
        posts_login = bool(re.search(r"<form[^>]+action=[\"'][^\"']*(?:take)?login\.php", text, re.I))
        return has_password and (has_username or posts_login)
