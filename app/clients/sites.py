"""站点接入的底层客户端：Torznab、RSS、M-Team、NexusPHP。"""

from __future__ import annotations

import base64
import html
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from defusedxml import ElementTree

from ..config import APP_VERSION, settings
from ..outbound import safe_request
from ..parsers import AccountTableParser, human_size_bytes, numeric_value, site_proxy
from ..util import to_int
from ._pool import _search_client


class TorznabClient:
    """Independent site adapter for Torznab-compatible endpoints (Prowlarr/Jackett/custom gateways)."""

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
    """Cookie 登录的站点：搜索与检测交给 ``app.sites`` 按站点档案处理，这里保留账户统计。"""

    # Some sites (e.g. hdarea.club) aggressively rate-limit automated User-Agents.
    _BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    async def search(self, site: dict[str, Any], title: str, imdb_id: str | None = None) -> list[dict[str, Any]]:
        from ..sites import search

        return await search(site, title, imdb_id)

    async def check(self, site: dict[str, Any]) -> dict[str, Any]:
        from ..sites import check

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
        """TNode SPA 站点的 /api/user/getInfo 用户信息。"""
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
        # TNode SPA：首页带 x-csrf-token，账户信息走 JSON API。
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
            # 反向：值 标签（如部分站点站“111.555 TB 上传量”，标签在数值之后、无冒号）
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
