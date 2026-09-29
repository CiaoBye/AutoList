"""从 CookieCloud 服务器（与 MoviePilot 共用）拉取站点 Cookie。

拉取有三个时机：定时（``PULL_INTERVAL_SECONDS``）、在设置页手动同步，以及站点报“Cookie 已失效”时
立即补拉一次。同一时间只有一次拉取在进行；失效补拉在 ``EXPIRED_REFRESH_COOLDOWN_SECONDS`` 内最多一次，
避免浏览器里的登录也已过期时，每次搜索都去拉一遍。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
from fastapi import HTTPException

from ..config import settings
from ..cookiecloud import cookie_groups, decrypt_cookiecloud
from ..database import connect
from ..logs import event_logger
from ..outbound import safe_request
from ..queries.sites import get_site
from ..schemas import COOKIECLOUD_KEY_PATTERN
from ..security import safe_error
from ..sites.errors import CookieExpired

PULL_INTERVAL_SECONDS = 10 * 60
EXPIRED_REFRESH_COOLDOWN_SECONDS = 2 * 60

T = TypeVar("T")
_pull_lock = asyncio.Lock()
_last_pull_monotonic: float | None = None


def cookiecloud_configured() -> bool:
    return bool(settings.cookiecloud_url and settings.cookiecloud_password and cookiecloud_key_valid())


def cookiecloud_key_valid() -> bool:
    return bool(re.fullmatch(COOKIECLOUD_KEY_PATTERN, (settings.cookiecloud_key or "").strip()))


def pull_due() -> bool:
    return _last_pull_monotonic is None or time.monotonic() - _last_pull_monotonic >= PULL_INTERVAL_SECONDS


def reset_pull_clock() -> None:
    """设置变更或测试时清除上次拉取的时间，下一轮定时任务会立即拉取。"""
    global _last_pull_monotonic
    _last_pull_monotonic = None


async def fetch_cookiecloud() -> dict[str, Any]:
    """从设置里的 CookieCloud 服务器取回密文并解密，返回 CookieCloud 原始数据。"""
    if not cookiecloud_configured():
        raise HTTPException(422, "请先在设置中填写 CookieCloud 服务器地址、用户 KEY 与端对端加密密码")
    uuid_value = settings.cookiecloud_key.strip()
    server = settings.cookiecloud_url.strip().rstrip("/")
    if server.endswith("/cookiecloud"):
        candidates = [f"{server}/get/{uuid_value}"]
    else:
        candidates = [f"{server}/get/{uuid_value}", f"{server}/cookiecloud/get/{uuid_value}"]
    last_error: Exception | None = None
    data: dict[str, Any] | None = None
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        for url in candidates:
            try:
                response = await safe_request(client, "GET", url, label="CookieCloud 服务器", allow_private=True)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                body = response.json()
                if isinstance(body, dict) and body.get("encrypted"):
                    data = body
                    break
            except Exception as exc:
                last_error = exc
    if data is None:
        reason = safe_error(last_error) if last_error else "未从服务器获取到有效 Cookie 密文，请确认服务器地址、用户 KEY 与端对端密码正确"
        raise HTTPException(502, f"拉取 CookieCloud 失败：{reason}")
    try:
        return decrypt_cookiecloud(uuid_value, settings.cookiecloud_password, data["encrypted"], data.get("crypto_type", "legacy"))
    except Exception as exc:
        raise HTTPException(422, f"CookieCloud 解密失败：{safe_error(exc)}") from exc


async def pull_cookiecloud(origin: str, site_id: int | None = None) -> dict[str, Any]:
    """拉取并更新站点 Cookie；``origin``（schedule / manual / expired）记入最近一次同步，指定站点时只更新它。"""
    async with _pull_lock:
        return await _pull_unlocked(origin, site_id)


async def refresh_expired_cookie(site: dict[str, Any]) -> bool:
    """站点报 Cookie 已失效：补拉一次 CookieCloud，这个站点的 Cookie 因此变化时写回 ``site`` 并返回 True。

    同时有多个请求发现失效时只拉一次；冷却期内不再拉取，只看库里的 Cookie 是否已被别的拉取更新。
    """
    if not cookiecloud_configured() or not site.get("id"):
        return False
    stale = str(site.get("cookie") or "")
    async with _pull_lock:
        recent = _last_pull_monotonic is not None and time.monotonic() - _last_pull_monotonic < EXPIRED_REFRESH_COOLDOWN_SECONDS
        if not recent:
            try:
                applied = await _pull_unlocked("expired")
            except Exception as exc:
                event_logger().warning(
                    "cookiecloud_expired_refresh_failed",
                    extra={"site": str(site.get("name") or ""), "error": safe_error(exc)},
                )
                return False
            event_logger().info(
                "cookiecloud_expired_refreshed",
                extra={
                    "site": str(site.get("name") or ""),
                    "total": len(applied["updated"]),
                    "detail": f"【{site.get('name')}】Cookie 已失效，已重新拉取 CookieCloud，更新 {len(applied['updated'])} 个站点",
                },
            )
    with connect() as conn:
        row = get_site(conn, int(site["id"]))
    fresh = str(row["cookie"] or "") if row else ""
    if not fresh or fresh == stale:
        return False
    site["cookie"] = fresh
    return True


async def _pull_unlocked(origin: str, site_id: int | None = None) -> dict[str, Any]:
    global _last_pull_monotonic
    from .sites import apply_cookie_groups

    payload = await fetch_cookiecloud()
    _last_pull_monotonic = time.monotonic()
    return apply_cookie_groups(cookie_groups(payload), site_id, origin=origin)


async def with_cookie_refresh(site: dict[str, Any], operation: Callable[[dict[str, Any]], Awaitable[T]]) -> T:
    """执行一次站点请求；站点报 Cookie 已失效且 CookieCloud 里有更新的 Cookie 时，换上新 Cookie 重试一次。"""
    try:
        return await operation(site)
    except CookieExpired:
        if not await refresh_expired_cookie(site):
            raise
        return await operation(site)
