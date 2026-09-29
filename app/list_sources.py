import csv
import html as html_lib
import io
import re
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx
from defusedxml import ElementTree as ET

from .config import settings
from .outbound import safe_request


ALLOWED_HOSTS = {
    "letterboxd.com", "www.letterboxd.com", "imdb.com", "www.imdb.com",
    "mdblist.com", "www.mdblist.com", "api.mdblist.com",
    "themoviedb.org", "www.themoviedb.org", "api.themoviedb.org",
}


def validate_source_url(raw_url: str) -> tuple[str, str]:
    url = raw_url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or host not in ALLOWED_HOSTS:
        raise ValueError("仅支持 TMDB、Letterboxd、IMDb 与 MDBList 的 HTTPS 片单地址")
    sensitive_keys = re.compile(r"(?:api.?key|token|passkey|secret|password|authorization)", re.I)
    if any(sensitive_keys.fullmatch(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError("片单地址不能包含 API Key、Token 或密码参数")
    return url, host


def parse_csv_items(content: str) -> tuple[str | None, list[dict[str, Any]]]:
    rows = list(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))))
    if len(rows) > 200_000:
        raise ValueError("CSV 行数超过上限（200000 行）")
    items: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        lowered = {str(key or "").strip().lower(): value for key, value in row.items()}
        title = next((lowered.get(key) for key in ("title", "original title", "original_title", "name") if lowered.get(key)), None)
        imdb_id = next((lowered.get(key) for key in ("const", "imdb id", "imdb_id", "imdb") if lowered.get(key)), None)
        tmdb_id = next((lowered.get(key) for key in ("tmdb id", "tmdb_id", "tmdb") if lowered.get(key)), None)
        if not title and not imdb_id and not tmdb_id:
            continue
        year = next((lowered.get(key) for key in ("year", "release year") if lowered.get(key)), None)
        rank = next((lowered.get(key) for key in ("position", "rank", "排名") if lowered.get(key)), index)
        items.append({
            "rank_no": int(rank) if str(rank or "").isdigit() else index,
            "imdb_id": str(imdb_id).strip() if imdb_id else None,
            "tmdb_id": int(tmdb_id) if str(tmdb_id or "").isdigit() else None,
            "original_title": str(title or imdb_id or f"TMDB {tmdb_id}").strip(),
            "year": int(year) if str(year or "").isdigit() else None,
            "chinese_title": None,
        })
    return None, items


LETTERBOXD_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
LETTERBOXD_FILM_LINK = re.compile(r"letterboxd\.com/(?:[^/]+/)?film/([a-z0-9]+(?:-[a-z0-9]+)*)/")

class PlaylistSourceFetcher:
    def __init__(self) -> None:
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/138.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }

    @staticmethod
    def _proxy() -> str | None:
        """片单来源（TMDB/MDBList/Letterboxd/IMDb）统一由 tmdb_proxy_enabled 门控（审计 3-8）。"""
        return settings.outbound_proxy_url if settings.tmdb_proxy_enabled and settings.outbound_proxy_url else None

    async def fetch(self, raw_url: str, limit: int = 5000) -> dict[str, Any]:
        url, host = validate_source_url(raw_url)
        limit = max(1, min(int(limit or 5000), 10000))
        if "themoviedb.org" in host:
            return await self._tmdb(url, limit)
        if "mdblist.com" in host:
            return await self._mdblist(url, limit)
        if "letterboxd.com" in host:
            return await self._letterboxd(url, limit)
        if "imdb.com" in host:
            return await self._imdb(url, limit)
        raise ValueError("无法识别片单来源")

    def _tmdb_auth(self) -> tuple[dict[str, str], dict[str, str]]:
        key = settings.tmdb_api_key.strip()
        if not key:
            raise ValueError("请先在设置中配置 TMDB API Key")
        if key.startswith("eyJ") or len(key) > 80:
            return {"Authorization": f"Bearer {key}"}, {}
        return {}, {"api_key": key}

    async def _tmdb(self, url: str, limit: int) -> dict[str, Any]:
        path = urlparse(url).path.strip("/")
        headers, auth = self._tmdb_auth()
        proxy = self._proxy()
        request_headers = {**self.headers, **headers}
        async with httpx.AsyncClient(timeout=30, proxy=proxy, follow_redirects=False) as client:
            collection = re.search(r"(?:^|/)collection/(\d+)", path)
            list_match = re.search(r"(?:^|/)list/(\d+)", path)
            if collection:
                response = await safe_request(
                    client, "GET",
                    f"https://api.themoviedb.org/3/collection/{collection.group(1)}",
                    headers=request_headers, params={**auth, "language": settings.tmdb_language},
                    label="TMDB 片单地址", proxy_mode=bool(proxy),
                )
                response.raise_for_status()
                data = response.json()
                entries = data.get("parts", [])[:limit]
                name = data.get("name")
            elif list_match:
                entries, page, name = [], 1, None
                while len(entries) < limit:
                    response = await safe_request(
                        client, "GET",
                        f"https://api.themoviedb.org/4/list/{list_match.group(1)}",
                        headers=request_headers, params={**auth, "page": page, "language": settings.tmdb_language},
                        label="TMDB 片单地址", proxy_mode=bool(proxy),
                    )
                    response.raise_for_status()
                    data = response.json()
                    name = name or data.get("name")
                    page_entries = data.get("results", [])
                    entries.extend(page_entries)
                    if not page_entries or page >= int(data.get("total_pages") or 1):
                        break
                    page += 1
                entries = entries[:limit]
            else:
                raise ValueError("TMDB 地址必须是 /list/{id} 或 /collection/{id}")
        items = []
        for index, entry in enumerate(entries, start=1):
            if entry.get("media_type") not in (None, "movie"):
                continue
            title = entry.get("original_title") or entry.get("title")
            if not title:
                continue
            year_text = str(entry.get("release_date") or "")[:4]
            items.append({"rank_no": index, "imdb_id": None, "tmdb_id": entry.get("id"), "original_title": title,
                          "year": int(year_text) if year_text.isdigit() else None, "chinese_title": entry.get("title")})
        return {"source_type": "tmdb", "source_name": name or "TMDB 片单", "source_url": url, "items": items}

    async def _mdblist(self, url: str, limit: int) -> dict[str, Any]:
        clean = url.split("?", 1)[0].rstrip("/")
        path = urlparse(clean).path.rstrip("/")
        if path.endswith("/json"):
            path = path[:-5]
        if path.endswith("/items"):
            path = path[:-6]
        api_url = f"https://api.mdblist.com{path}/items"
        params: dict[str, Any] = {"limit": min(limit, 1000), "offset": 0}
        if settings.mdblist_api_key:
            params["apikey"] = settings.mdblist_api_key
        proxy = self._proxy()
        async with httpx.AsyncClient(timeout=30, proxy=proxy, follow_redirects=False) as client:
            response = await safe_request(
                client, "GET", api_url, headers=self.headers, params=params,
                label="MDBList 片单地址", proxy_mode=bool(proxy),
            )
            if response.status_code >= 400:
                response = await safe_request(
                    client, "GET", f"{clean}/json", headers=self.headers,
                    params={"limit": limit, "offset": 0}, label="MDBList 片单地址",
                    proxy_mode=bool(proxy),
                )
            response.raise_for_status()
            data = response.json()
        entries = data if isinstance(data, list) else data.get("movies") or data.get("items") or []
        items = []
        for index, entry in enumerate(entries[:limit], start=1):
            title = entry.get("title") or entry.get("name")
            ids = entry.get("ids") or {}
            imdb_id = entry.get("imdb_id") or ids.get("imdb")
            tmdb_id = entry.get("tmdb_id") or ids.get("tmdb") or (entry.get("id") if entry.get("mediatype") == "movie" else None)
            if not title and not imdb_id and not tmdb_id:
                continue
            year = entry.get("year") or entry.get("release_year")
            items.append({"rank_no": index, "imdb_id": imdb_id, "tmdb_id": tmdb_id,
                          "original_title": title or imdb_id or f"TMDB {tmdb_id}",
                          "year": int(year) if str(year or "").isdigit() else None, "chinese_title": None})
        return {"source_type": "mdblist", "source_name": "MDBList 片单", "source_url": url, "items": items}

    async def _letterboxd(self, url: str, limit: int) -> dict[str, Any]:
        base = url.split("?", 1)[0].rstrip("/")
        segments = urlparse(base).path.strip("/").split("/")
        if "list" not in segments or segments.index("list") + 1 >= len(segments):
            raise ValueError("Letterboxd 地址必须是公开片单 URL")
        username, slug = segments[0], segments[segments.index("list") + 1]
        all_items: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        name = "Letterboxd 片单"
        proxy = self._proxy()
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, proxy=proxy) as client:
            rss = await safe_request(
                client, "GET", f"https://letterboxd.com/{username}/list/{slug}/rss/",
                headers=self.headers, label="Letterboxd 片单地址", proxy_mode=bool(proxy),
            )
            if rss.is_success and "xml" in rss.headers.get("content-type", ""):
                try:
                    root = ET.fromstring(rss.text)
                    channel_title = root.findtext("./channel/title")
                    name = channel_title.strip() if channel_title else name
                    for entry in root.findall("./channel/item"):
                        title_text = (entry.findtext("title") or "").strip()
                        year_match = re.search(r",\s*(\d{4})$", title_text)
                        title = re.sub(r",\s*\d{4}$", "", title_text).strip()
                        slug_match = LETTERBOXD_FILM_LINK.search(entry.findtext("link") or "")
                        tmdb_text = next((child.text for child in entry if child.tag.endswith("}movieId") and child.text), None)
                        if title:
                            all_items.append({"rank_no": len(all_items) + 1, "imdb_id": None,
                                              "tmdb_id": int(tmdb_text) if str(tmdb_text or "").strip().isdigit() else None,
                                              "source_ref": f"letterboxd:{slug_match.group(1)}" if slug_match else None,
                                              "original_title": title, "year": int(year_match.group(1)) if year_match else None, "chinese_title": None})
                            if len(all_items) >= limit:
                                break
                except ET.ParseError:
                    rss_parsed = False  # RSS 片段解析失败时回退到页面抓取
            if all_items:
                return {"source_type": "letterboxd", "source_name": name, "source_url": url, "items": all_items}
            embed_base = f"https://embed.letterboxd.com/{username}/list/{slug}"
            while len(all_items) < limit:
                page_url = f"{embed_base}/" if page == 1 else f"{embed_base}/page/{page}/"
                response = await safe_request(
                    client, "GET", page_url, headers=self.headers,
                    label="Letterboxd 片单地址", proxy_mode=bool(proxy),
                )
                response.raise_for_status()
                html = response.text
                if page == 1:
                    title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
                    if title_match:
                        decoded_title = html_lib.unescape(html_lib.unescape(title_match.group(1)))
                        name = re.sub(r",\s*a list of films by .*?$", "", re.sub(r"\s*[•·|-]\s*Letterboxd.*$", "", re.sub(r"\s+", " ", decoded_title)), flags=re.I).replace("\u200e", "").strip()
                cards = []
                for tag in re.findall(r'<div\b[^>]*\bdata-item-slug="[^"]+"[^>]*>', html, re.I):
                    slug_match = re.search(r'data-item-slug="([^"]+)"', tag, re.I)
                    label_match = re.search(r'data-item-(?:full-display-name|name)="([^"]+)"', tag, re.I)
                    if slug_match and label_match:
                        cards.append((slug_match.group(1), html_lib.unescape(html_lib.unescape(label_match.group(1)))))
                if not cards:
                    cards = re.findall(r'data-target-link="/film/([^/]+)/"[^>]*>.*?alt="([^"]+)"', html, re.I | re.S)
                if not cards:
                    cards = [(slug, slug.replace("-", " ").title()) for slug in re.findall(r'data-film-slug="([^"]+)"', html, re.I)]
                added = 0
                for slug, label in cards:
                    if slug in seen:
                        continue
                    seen.add(slug)
                    year_match = re.search(r"\((\d{4})\)\s*$", label)
                    title = re.sub(r"^Poster for\s+", "", re.sub(r"\s*\(\d{4}\)\s*$", "", label), flags=re.I).strip()
                    all_items.append({"rank_no": len(all_items) + 1, "imdb_id": None, "tmdb_id": None,
                                      "source_ref": f"letterboxd:{slug}" if LETTERBOXD_SLUG.fullmatch(slug) else None,
                                      "original_title": title, "year": int(year_match.group(1)) if year_match else None, "chinese_title": None})
                    added += 1
                    if len(all_items) >= limit:
                        break
                if added == 0 or len(all_items) >= limit or not re.search(rf"/page/{page + 1}/", html):
                    break
                page += 1
        return {"source_type": "letterboxd", "source_name": name, "source_url": url, "items": all_items}

    async def letterboxd_film_ids(self, slug: str) -> tuple[int | None, str | None]:
        """Letterboxd 影片页记录的 TMDB 与 IMDb 编号（Letterboxd 的影片资料来自 TMDB）。

        letterboxd.com 有 Cloudflare 校验，官方嵌入域名 embed.letterboxd.com 的影片页可以直接读取。
        """
        if not LETTERBOXD_SLUG.fullmatch(slug):
            raise ValueError("Letterboxd 影片标识无效")
        proxy = self._proxy()
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, proxy=proxy) as client:
            response = await safe_request(
                client, "GET", f"https://embed.letterboxd.com/film/{slug}/", headers=self.headers,
                label="Letterboxd 影片地址", proxy_mode=bool(proxy),
            )
            response.raise_for_status()
        html = response.text
        tmdb_type = re.search(r'data-tmdb-type="([a-z]+)"', html)
        tmdb_id = re.search(r'data-tmdb-id="(\d+)"', html)
        imdb_id = re.search(r"imdb\.com/title/(tt\d{5,10})", html)
        is_movie = not tmdb_type or tmdb_type.group(1) == "movie"
        return (int(tmdb_id.group(1)) if tmdb_id and is_movie else None), (imdb_id.group(1) if imdb_id else None)

    async def _imdb(self, url: str, limit: int) -> dict[str, Any]:
        path = urlparse(url).path.rstrip("/")
        list_match = re.search(r"/list/(ls\d+)", path)
        if list_match:
            list_id = list_match.group(1)
            name = f"IMDb {list_match.group(1)}"
        elif re.search(r"/user/ur\d+/watchlist", path):
            raise ValueError("IMDb Watchlist 请使用公开 List 链接或导出的 CSV；个人 Watchlist 需要登录授权")
        else:
            raise ValueError("IMDb 地址必须是公开 List 或 Watchlist")
        query = """query ImdbList($id: ID!, $first: Int, $after: ID) { list(id: $id) { name { originalText } items(first: $first, after: $after, sort: {by: LIST_ORDER, order: ASC}) { edges { position node { absolutePosition listItem { ... on Title { id titleText { text } originalTitleText { text } releaseYear { year } titleType { id } } } } } pageInfo { hasNextPage endCursor } } } }"""
        headers = {**self.headers, "Content-Type": "application/json", "x-imdb-client-name": "imdb-web-next"}
        items: list[dict[str, Any]] = []
        cursor = None
        proxy = self._proxy()
        async with httpx.AsyncClient(timeout=30, proxy=proxy, follow_redirects=False) as client:
            while len(items) < limit:
                response = await safe_request(
                    client, "POST", "https://api.graphql.imdb.com/", headers=headers,
                    json={"query": query, "variables": {"id": list_id, "first": min(250, limit - len(items)), "after": cursor}},
                    label="IMDb 片单地址", proxy_mode=bool(proxy),
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("errors"):
                    raise ValueError(payload["errors"][0].get("message") or "IMDb GraphQL 解析失败")
                list_data = (payload.get("data") or {}).get("list") or {}
                name = ((list_data.get("name") or {}).get("originalText") or name).strip()
                connection = list_data.get("items") or {}
                for edge in connection.get("edges") or []:
                    title = ((edge.get("node") or {}).get("listItem") or {})
                    if (title.get("titleType") or {}).get("id") not in ("movie", "tvMovie"):
                        continue
                    display = (title.get("originalTitleText") or title.get("titleText") or {}).get("text")
                    if not display:
                        continue
                    items.append({"rank_no": len(items) + 1, "imdb_id": title.get("id"), "tmdb_id": None,
                                  "original_title": display, "year": (title.get("releaseYear") or {}).get("year"), "chinese_title": None})
                page_info = connection.get("pageInfo") or {}
                cursor = page_info.get("endCursor")
                if not page_info.get("hasNextPage") or not cursor:
                    break
        if not items:
            raise ValueError("IMDb 公开片单没有返回电影条目")
        return {"source_type": "imdb", "source_name": name, "source_url": url, "items": items[:limit]}
