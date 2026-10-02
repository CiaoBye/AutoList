"""站点搜索与检测的入口：按档案选择解析方式，统一请求、限额与错误。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from ..config import APP_VERSION
from ..parsers import site_proxy
from ..util import to_int
from ..outbound import safe_request
from . import official_api_site, nexusphp
from .profiles import profile_for

# 连接检测用一部各站普遍收录的电影做真实搜索，确认能解析出结果，而不只是 Cookie 能登录。
PROBE_TITLE, PROBE_IMDB = "The Godfather", "tt0068646"


def _user_agent(site: dict[str, Any]) -> str:
    return str(site.get("user_agent") or f"AutoList/{APP_VERSION}")


async def search(site: dict[str, Any], title: str, imdb_id: str | None = None) -> list[dict[str, Any]]:
    """按站点档案搜索一次，返回寻片流程使用的种子字典。"""
    from ..clients import _search_client  # 共享连接池；延迟导入避免与 clients 互相引用

    base = str(site.get("base_url") or "").rstrip("/") + "/"
    api_key = str(site.get("api_key") or "").strip()
    profile = profile_for(base, has_api_key=bool(api_key))
    user_agent = _user_agent(site)
    proxy = site_proxy(site)
    client = _search_client(timeout=to_int(site.get("timeout_seconds") or 30), proxy=proxy)
    label = f"站点 {site.get('name', '')} 地址"
    if profile.framework == "official_api":
        response = await safe_request(
            client, "POST", official_api_site.search_url(base), json=official_api_site.request_body(title, imdb_id),
            headers={"x-api-key": api_key, "User-Agent": user_agent, "Content-Type": "application/json"},
            label=label, proxy_mode=bool(proxy),
        )
        response.raise_for_status()
        torrents = official_api_site.parse_results(response.json(), base)
    else:
        headers = {"Cookie": str(site.get("cookie") or ""), "User-Agent": user_agent}
        response = await safe_request(
            client, "GET", urljoin(base, profile.search_path), params=nexusphp.build_params(profile, title, imdb_id),
            headers=headers, label=label, proxy_mode=bool(proxy),
        )
        response.raise_for_status()
        nexusphp.detect_interruption(response.url.path or "", response.text)
        torrents = nexusphp.parse_page(response.text, profile, base)
    return [torrent.as_candidate(site, user_agent) for torrent in torrents]


def _same_site(url: str, base: str) -> bool:
    host, base_host = (urlsplit(url).hostname or "").lower(), (urlsplit(base).hostname or "").lower()
    return bool(host) and (host == base_host or host.endswith("." + base_host) or base_host.endswith("." + host))


async def verify(site: dict[str, Any], detail_url: str | None) -> bool | None:
    """提交前回详情页确认种子仍在站点上。

    返回 True 表示仍然存在，False 表示已被站点删除，None 表示这类站点无法确认（官方 API
    站点、没有详情页或详情页不在本站）；Cookie 失效、二次验证、网络故障等抛出异常，由调用方暂缓提交。
    """
    present, _ = await _details(site, detail_url)
    return present


async def verify_and_refresh(site: dict[str, Any], detail_url: str | None, enclosure: str | None) -> tuple[bool | None, str | None]:
    """同 ``verify``，并在下载地址带时效签名时从详情页取当前有效的地址（取不到为 None）。"""
    present, html = await _details(site, detail_url)
    if not present or not nexusphp.is_signed_download(enclosure):
        return present, None
    torrent_id = (parse_qs(urlsplit(str(enclosure)).query).get("id") or [""])[0]
    base = str(site.get("base_url") or "").rstrip("/") + "/"
    return present, nexusphp.fresh_signed_download(html or "", torrent_id, base)


async def _details(site: dict[str, Any], detail_url: str | None) -> tuple[bool | None, str | None]:
    from ..clients import _search_client

    base = str(site.get("base_url") or "").rstrip("/") + "/"
    profile = profile_for(base, has_api_key=bool(str(site.get("api_key") or "").strip()))
    if profile.framework != "nexusphp" or not detail_url:
        return None, None
    url = urljoin(base, detail_url)
    # 请求带着站点 Cookie，只发往站点自己的域名。
    if not _same_site(url, base):
        return None, None
    proxy = site_proxy(site)
    client = _search_client(timeout=to_int(site.get("timeout_seconds") or 30), proxy=proxy)
    response = await safe_request(
        client, "GET", url, headers={"Cookie": str(site.get("cookie") or ""), "User-Agent": _user_agent(site)},
        label=f"站点 {site.get('name', '')} 地址", proxy_mode=bool(proxy),
    )
    if nexusphp.torrent_deleted(response.status_code, ""):
        return False, None
    response.raise_for_status()
    nexusphp.detect_interruption(response.url.path or "", response.text)
    return not nexusphp.torrent_deleted(response.status_code, response.text), response.text


def search_page_url(base_url: str) -> str:
    """站点的种子搜索页，需要人机验证时让用户在浏览器里打开它完成验证。"""
    base = str(base_url or "").rstrip("/") + "/"
    return urljoin(base, profile_for(base).search_path)


async def check(site: dict[str, Any]) -> dict[str, Any]:
    """真实搜索一次：先按 IMDb，搜不到再按片名；两次都没有结果才算“搜不到”。"""
    results = await search(site, PROBE_TITLE, PROBE_IMDB)
    if not results:
        results = await search(site, PROBE_TITLE)
    if not results:
        return {
            "ok": True, "empty": True,
            "message": f"登录正常，但搜索《{PROBE_TITLE}》没有解析到结果：站点可能不收录电影，或页面结构暂不兼容",
        }
    return {"ok": True, "message": f"可以搜索：《{PROBE_TITLE}》解析到 {len(results)} 条结果"}
