import base64
import asyncio
import html
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from defusedxml import ElementTree

from .config import settings


class NexusTableParser(HTMLParser):
    """Collect torrent rows while preserving outer cells across nested tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self.stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "tr":
            self.stack.append({"cells": [], "td_depth": 0, "links": [], "anchors": [], "free": False})
            return
        for row in self.stack:
            if tag.lower() == "td":
                row["td_depth"] += 1
                if row["td_depth"] == 1:
                    row["cells"].append([])
            elif tag.lower() == "a":
                link = {"href": html.unescape(attributes.get("href", "")), "title": attributes.get("title", ""), "text": []}
                row["links"].append(link)
                row["anchors"].append(link)
            marker = " ".join((attributes.get("class", ""), attributes.get("src", "")))
            if re.search(r"(?:^|[\s_/.-])(pro_free|free2up)(?:[\s_/.-]|$)", marker, re.I):
                row["free"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "tr":
            if self.stack:
                self.rows.append(self.stack.pop())
            return
        for row in self.stack:
            if tag.lower() == "td" and row["td_depth"]:
                row["td_depth"] -= 1
            elif tag.lower() == "a" and row["anchors"]:
                row["anchors"].pop()

    def handle_data(self, data: str) -> None:
        for row in self.stack:
            if row["td_depth"] and row["cells"]:
                row["cells"][-1].append(data)
            if row["anchors"]:
                row["anchors"][-1]["text"].append(data)


def site_proxy(site: dict[str, Any]) -> str | None:
    """Only explicitly opted-in PT sites use the configured outbound proxy."""
    return settings.outbound_proxy_url if settings.pt_proxy_enabled and site.get("proxy") and settings.outbound_proxy_url else None


class MoviePilotClient:
    """Optional MoviePilot compatibility gateway for PT aggregation or smart download."""

    def __init__(self) -> None:
        self.base_url = settings.mp_base_url
        self.headers = {"X-API-KEY": settings.mp_api_key} if settings.mp_api_key else {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=settings.mp_timeout_seconds)

    async def check(self) -> dict[str, Any]:
        if not self.base_url or not settings.mp_api_key:
            return {"ok": False, "configured": False, "message": "未配置 MoviePilot"}
        async with self._client() as client:
            response = await client.get("/api/v1/download/clients")
            response.raise_for_status()
            return {"ok": True, "configured": True, "downloaders": response.json()}

    async def search_title(self, title: str) -> Any:
        async with self._client() as client:
            response = await client.get("/api/v1/search/title", params={"keyword": title, "page": 0})
            response.raise_for_status()
            return response.json()

    async def search_media(self, tmdb_id: int, title: str, year: int | None, site_ids: list[int] | None = None) -> Any:
        async with self._client() as client:
            response = await client.get(
                f"/api/v1/search/media/tmdb:{tmdb_id}",
                params={"mtype": "电影", "title": title, "year": str(year) if year else None, "sites": ",".join(map(str, site_ids)) if site_ids else None},
            )
            response.raise_for_status()
            return response.json()

    async def sites(self) -> Any:
        async with self._client() as client:
            response = await client.get("/api/v1/site/")
            response.raise_for_status()
            return response.json()

    async def site_statistics(self) -> list[dict[str, Any]]:
        async with self._client() as client:
            response = await client.get("/api/v1/site/statistic")
            response.raise_for_status()
            return response.json()

    async def site_user_data(self) -> list[dict[str, Any]]:
        async with self._client() as client:
            response = await client.get("/api/v1/site/userdata/latest")
            response.raise_for_status()
            return response.json()

    async def refresh_site_user_data(self, site_id: int) -> Any:
        async with self._client() as client:
            response = await client.post(f"/api/v1/site/userdata/{site_id}")
            response.raise_for_status()
            return response.json()

    async def update_site_cookie(self, site_id: int, username: str, password: str, code: str = "") -> Any:
        async with self._client() as client:
            response = await client.post(f"/api/v1/site/cookie/{site_id}", json={"username": username, "password": password, "code": code or None})
            response.raise_for_status()
            return response.json()

    async def site_icon(self, site_id: int) -> Any:
        async with self._client() as client:
            response = await client.get(f"/api/v1/site/icon/{site_id}")
            response.raise_for_status()
            return response.json()

    async def custom_release_groups(self) -> list[str]:
        """One-time migration helper; candidate parsing never depends on MP at runtime."""
        async with self._client() as client:
            response = await client.get("/api/v1/system/setting/CustomReleaseGroups")
            response.raise_for_status()
            payload = response.json()
        value = payload.get("data", {}).get("value") if isinstance(payload, dict) else None
        return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []

    async def download(self, media_in: dict[str, Any], torrent_in: dict[str, Any], downloader: str | None = None) -> Any:
        torrent_payload = dict(torrent_in)
        factor = torrent_payload.pop("volume_factor", None)
        if factor is not None and torrent_payload.get("downloadvolumefactor") is None:
            torrent_payload["downloadvolumefactor"] = float(factor)
        publish_time = torrent_payload.pop("publish_time", None)
        if publish_time and not torrent_payload.get("pubdate"):
            torrent_payload["pubdate"] = str(publish_time)
        async with self._client() as client:
            response = await client.post(
                "/api/v1/download/",
                json={"media_in": media_in, "torrent_in": torrent_payload, "downloader": downloader},
            )
            response.raise_for_status()
            return response.json()

    async def add_download(self, torrent_in: dict[str, Any], tmdb_id: int | None = None) -> Any:
        async with self._client() as client:
            response = await client.post("/api/v1/download/add", json={"torrent_in": torrent_in, "tmdbid": tmdb_id})
            response.raise_for_status()
            return response.json()


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
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=settings.mp_timeout_seconds,
                                             proxy=settings.outbound_proxy_url if settings.tmdb_proxy_enabled and settings.outbound_proxy_url else None) as client:
                    response = await client.get(path, params=params)
                    response.raise_for_status()
                    return response
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"TMDB 网络连接失败：{type(last_error).__name__}") from last_error

    async def check(self) -> dict[str, Any]:
        if not settings.tmdb_api_key:
            return {"ok": False, "configured": False, "message": "未配置 TMDB API Key"}
        _, params = self._auth()
        await self._get("/configuration", params)
        return {"ok": True, "configured": True}

    async def search_movie(self, title: str, year: int | None = None) -> list[dict[str, Any]]:
        if not settings.tmdb_api_key:
            raise RuntimeError("请先在设置中填写 TMDB API Key")
        _, auth_params = self._auth()
        params: dict[str, Any] = {"query": title, "language": settings.tmdb_language, **auth_params}
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
            response = await client.post("/chat/completions", json=payload)
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
        async with httpx.AsyncClient(base_url=settings.emby_base_url, timeout=settings.mp_timeout_seconds) as client:
            response = await client.get("/emby/System/Info", params=self._params())
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
        async with httpx.AsyncClient(base_url=settings.emby_base_url, timeout=settings.mp_timeout_seconds) as client:
            exact_matches: list[dict[str, Any]] = []
            for provider, provider_id in (("tmdb", tmdb_id), ("imdb", imdb_id)):
                if not provider_id:
                    continue
                response = await client.get(
                    "/emby/Items",
                    params={**base_params, "AnyProviderIdEquals": f"{provider}.{provider_id}"},
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
            response = await client.get("/emby/Items", params={**base_params, "SearchTerm": title})
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

    async def library_state(
        self, title: str, year: int | None, tmdb_id: int | None = None, imdb_id: str | None = None,
    ) -> str:
        state, _ = await self.library_match(title, year, tmdb_id, imdb_id)
        return state

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
        async with httpx.AsyncClient(base_url=settings.emby_base_url, timeout=settings.mp_timeout_seconds) as client:
            response = await client.get(
                f"/emby/Items/{item_id}/Images/Primary",
                params={**self._params(), "maxHeight": 720, "quality": 90},
            )
            response.raise_for_status()
        return response.content, response.headers.get("content-type", "image/jpeg").split(";", 1)[0]


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
        self.session_id = ""

    async def _rpc(self, method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("未配置 Transmission 地址")
        headers = {"X-Transmission-Session-Id": self.session_id} if self.session_id else {}
        async with httpx.AsyncClient(auth=self.auth, timeout=settings.mp_timeout_seconds) as client:
            response = await client.post(self.base_url, headers=headers, json={"method": method, "arguments": arguments or {}})
            if response.status_code == 409:
                self.session_id = response.headers.get("X-Transmission-Session-Id", "")
                response = await client.post(
                    self.base_url,
                    headers={"X-Transmission-Session-Id": self.session_id},
                    json={"method": method, "arguments": arguments or {}},
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
        fields = ["id", "name", "hashString", "status", "percentDone", "totalSize", "labels", "downloadDir"]
        data = await self._rpc("torrent-get", {"fields": fields})
        return data.get("torrents", [])


class TorznabClient:
    """Independent PT adapter for Torznab-compatible endpoints (Prowlarr/Jackett/custom gateways)."""

    async def search(self, site: dict[str, Any], title: str, imdb_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"t": "movie", "q": title, "apikey": site.get("api_key", "")}
        if imdb_id:
            params["imdbid"] = imdb_id.removeprefix("tt")
        async with httpx.AsyncClient(timeout=settings.mp_timeout_seconds, follow_redirects=True, proxy=site_proxy(site)) as client:
            response = await client.get(site["base_url"], params=params)
            response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        results: list[dict[str, Any]] = []
        for item in root.findall(".//item"):
            attrs = {node.attrib.get("name"): node.attrib.get("value") for node in item if node.tag.endswith("attr")}
            enclosure = item.find("enclosure")
            results.append({
                "title": item.findtext("title") or "未知资源",
                "site_name": site["name"],
                "enclosure": enclosure.attrib.get("url") if enclosure is not None else item.findtext("link"),
                "size": int(attrs.get("size") or item.findtext("size") or 0),
                "seeders": int(attrs.get("seeders") or 0),
                "publish_time": item.findtext("pubDate"),
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
        async with httpx.AsyncClient(
            timeout=int(site.get("timeout_seconds") or 30), follow_redirects=True, proxy=site_proxy(site),
        ) as client:
            response = await client.get(feed_url, headers=headers)
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
            size = int((enclosure.attrib.get("length") if enclosure is not None else "0") or 0)
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
            "User-Agent": str(site.get("user_agent") or "AutoList/0.65"),
        }

    async def search(self, site: dict[str, Any], title: str, imdb_id: str | None = None) -> list[dict[str, Any]]:
        keyword = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else title
        payload = {"pageNumber": 1, "pageSize": 100, "mode": "normal", "keyword": keyword}
        timeout = int(site.get("timeout_seconds") or 30)
        async with httpx.AsyncClient(timeout=timeout, proxy=site_proxy(site)) as client:
            response = await client.post(f"{self.api_base(site)}/api/torrent/search", headers=self.headers(site), json=payload)
            response.raise_for_status()
            body = response.json()
        if str(body.get("code")) not in ("0", "None") and body.get("message") != "SUCCESS":
            raise RuntimeError(body.get("message") or "M-Team API 搜索失败")
        rows = ((body.get("data") or {}).get("data") or [])
        results: list[dict[str, Any]] = []
        for row in rows:
            status = row.get("status") or {}
            discount = (status.get("promotionRule") or {}).get("discount") or ("FREE" if status.get("mallSingleFree") else status.get("discount")) or "NORMAL"
            factor = {"FREE": 0, "PERCENT_50": 0.5, "PERCENT_70": 0.7}.get(discount, 1)
            labels = list(row.get("labelsNew") or [])
            if factor == 0:
                labels.insert(0, "FREE")
            elif factor < 1:
                labels.insert(0, f"{int(factor * 100)}%")
            request_options = {
                "method": "post", "cookie": False, "params": {"id": str(row.get("id"))},
                "header": {**self.headers(site), "Content-Type": "multipart/form-data"}, "result": "data",
            }
            encoded = base64.b64encode(json.dumps(request_options).encode()).decode()
            results.append({
                "title": row.get("name") or "未知资源", "description": row.get("smallDescr"),
                "site_name": site["name"], "size": int(row.get("size") or 0),
                "seeders": int(status.get("seeders") or 0), "leechers": int(status.get("leechers") or 0),
                "enclosure": f"[{encoded}]{self.api_base(site)}/api/torrent/genDlToken",
                "labels": labels, "volume_factor": factor, "site_ua": site.get("user_agent") or "AutoList/0.65",
            })
        return results

    async def check(self, site: dict[str, Any]) -> dict[str, Any]:
        results = await self.search(site, "AutoListConnectionProbe")
        return {"ok": True, "message": f"API 可用，探测返回 {len(results)} 条"}


class NexusPHPClient:
    """Conservative cookie-based adapter for common NexusPHP torrent tables."""

    # Some sites use different search page endpoints.
    _SEARCH_PATHS: dict[str, str] = {
        "tracker-a.example": "browse.php",
    }
    # Some sites (e.g. hdarea.club) aggressively rate-limit automated User-Agents.
    _BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    @staticmethod
    def _text(fragment: str) -> str:
        plain = re.sub(r"<[^>]+>", " ", fragment)
        return re.sub(r"\s+", " ", html.unescape(plain)).strip()

    @staticmethod
    def _size(cells: list[str]) -> int:
        units = {
            "b": 1, "kb": 1024, "kib": 1024, "mb": 1024**2, "mib": 1024**2,
            "gb": 1024**3, "gib": 1024**3, "tb": 1024**4, "tib": 1024**4,
        }
        for value in cells:
            match = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(B|Ki?B|Mi?B|Gi?B|Ti?B)\b", value, re.I)
            if match:
                number = float(match.group(1).replace(",", "."))
                return int(number * units[match.group(2).lower()])
        return 0

    async def search(self, site: dict[str, Any], title: str, imdb_id: str | None = None) -> list[dict[str, Any]]:
        base = str(site.get("base_url") or "").rstrip("/") + "/"
        host = (urlparse(base).hostname or "").lower()
        search_page = "torrents.php"
        for domain, path in self._SEARCH_PATHS.items():
            if domain in host:
                search_page = path
                break
        params = {"search": imdb_id or title, "search_area": 0}
        headers = {"Cookie": str(site.get("cookie") or ""), "User-Agent": str(site.get("user_agent") or "AutoList/0.65")}
        async with httpx.AsyncClient(timeout=int(site.get("timeout_seconds") or 30), follow_redirects=True, proxy=site_proxy(site)) as client:
            response = await client.get(urljoin(base, search_page), params=params, headers=headers)
            response.raise_for_status()
        results: list[dict[str, Any]] = []
        parser = NexusTableParser()
        parser.feed(response.text)
        for row in parser.rows:
            detail = next((link for link in row["links"] if re.search(r"details\.php\?[^#]*\bid=\d+", link["href"], re.I)), None)
            if not detail:
                continue
            download = next((link for link in row["links"] if re.search(r"download\.php\?", link["href"], re.I)), None)
            if not download:
                continue
            title_text = self._text(detail["title"] or " ".join(detail["text"]))
            cells = [self._text(" ".join(cell)) for cell in row["cells"]]
            numeric = [int(value.replace(",", "")) for value in cells[-5:] if re.fullmatch(r"[\d,]+", value)]
            results.append({
                "title": title_text, "site_name": site["name"], "size": self._size(cells),
                "seeders": numeric[-3] if len(numeric) >= 3 else 0,
                "enclosure": urljoin(base, download["href"]), "labels": ["FREE"] if row["free"] else [],
                "volume_factor": 0 if row["free"] else 1, "site_cookie": site.get("cookie") or "", "site_ua": headers["User-Agent"],
            })
        unique: dict[str, dict[str, Any]] = {}
        for item in results:
            key = str(item.get("enclosure") or item.get("title") or "")
            current = unique.get(key)
            if current is None or int(item.get("size") or 0) > int(current.get("size") or 0):
                unique[key] = item
        return list(unique.values())

    async def check(self, site: dict[str, Any]) -> dict[str, Any]:
        results = await self.search(site, "AutoListConnectionProbe")
        return {"ok": True, "message": f"Cookie 可用，解析到 {len(results)} 条探测结果"}
