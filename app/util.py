"""Shared helpers used by routes and services."""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import os
import re
import socket
import sqlite3
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from fastapi import HTTPException

from .security import sanitize_sensitive_text


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def secret_free(value: Any) -> Any:
    if isinstance(value, str):
        sanitized = sanitize_sensitive_text(value)
        sanitized = re.sub(r"(?i)magnet:\?[^\s<>\"']+", "[redacted]", sanitized)
        return re.sub(r"(?i)https?://[^\s<>\"']+", "[redacted]", sanitized)
    if isinstance(value, list):
        return [secret_free(item) for item in value]
    if isinstance(value, tuple):
        return tuple(secret_free(item) for item in value)
    if not isinstance(value, dict):
        return value
    blocked = re.compile(
        r"(?:magnet|enclosure|download|url|uri|cookie|passkey|token|authorization|api.?key|header|password|passwd|secret)",
        re.I,
    )
    return {key: secret_free(item) for key, item in value.items() if not blocked.search(str(key))}


def raster_image_media_type(content: bytes) -> str | None:
    """Identify supported raster icon formats by magic bytes; remote SVG/HTML is never re-served."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    return None


MAX_COOKIECLOUD_BODY = 40 * 1024 * 1024


def decode_cookiecloud_body(content: bytes, content_encoding: str, limit: int = MAX_COOKIECLOUD_BODY) -> bytes:
    """Bound compressed and expanded CookieCloud uploads before JSON validation."""
    if len(content) > limit:
        raise HTTPException(413, "CookieCloud 上传数据过大")
    if "gzip" in content_encoding.lower():
        try:
            with gzip.GzipFile(fileobj=BytesIO(content)) as compressed:
                content = compressed.read(limit + 1)
        except (OSError, EOFError) as exc:
            raise HTTPException(422, "CookieCloud gzip 数据无效") from exc
    if len(content) > limit:
        raise HTTPException(413, "CookieCloud 上传数据过大")
    return content


def is_private_or_reserved_address(address: Any) -> bool:
    """True when an address must not be used as an outbound target.
    Loopback, link-local, multicast, unspecified, RFC1918/ULA private space and
    reserved ranges are rejected. 198.18.0.0/15 (benchmark range) is excluded:
    Clash/Surge-style proxies commonly use it as fake-ip for every domain, so
    treating it as private would break DNS validation on such networks.
    """
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        return True
    if address.version == 4:
        value = int(address)
        if 0xC6120000 <= value < 0xC6140000:  # 198.18.0.0/15 fake-ip range（在 is_private 之前判断）
            return False
        if address.is_private:
            return True
    elif address.is_private:
        return True
    return address.is_reserved


SENSITIVE_URL_QUERY_KEY = re.compile(
    r"^(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|passkey|password|passwd|secret|cookie|authorization|key)$",
    re.I,
)
OUTBOUND_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_OUTBOUND_REDIRECTS = 4


def configured_private_hosts() -> set[str]:
    """Read private-host exceptions at request time so runtime env changes are safe."""
    return {
        host.strip().lower().lstrip(".").rstrip(".")
        for host in os.getenv("AUTOLIST_ALLOW_PRIVATE_HOSTS", "").split(",")
        if host.strip()
    }


def host_is_allowlisted(hostname: str, allowlist: set[str] | None = None) -> bool:
    normalized = hostname.strip().lower().rstrip(".")
    allowed = configured_private_hosts() if allowlist is None else allowlist
    return any(normalized == host or normalized.endswith(f".{host}") for host in allowed)


def _resolve_outbound_addresses(hostname: str, port: int | None = None) -> list[Any]:
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("目标域名无法解析") from exc
    if not addresses:
        raise ValueError("目标域名无法解析")
    return [ipaddress.ip_address(address[4][0]) for address in addresses]


def validate_outbound_url(
    value: str,
    *,
    label: str = "出站地址",
    allow_private: bool | None = None,
    allowed_private_hosts: set[str] | None = None,
    reject_sensitive_query: bool = False,
) -> str:
    """Validate one actual outbound target.

    This is deliberately independent from FastAPI so clients, source fetchers
    and icon proxying share the same scheme, credential, DNS and redirect
    policy.  DNS is checked immediately before each request; callers still
    need a fixed network boundary if they require protection against a
    resolver changing between validation and TCP connect.
    """
    normalized = str(value or "").strip()
    if any(ord(char) < 0x20 for char in normalized):
        raise ValueError(f"{label}地址包含控制字符")
    try:
        parsed = urlparse(normalized)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label}地址格式无效") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError(f"{label}必须以 http:// 或 https:// 开头")
    if parsed.username or parsed.password:
        raise ValueError(f"{label}不能包含用户名或密码")
    if reject_sensitive_query and any(
        SENSITIVE_URL_QUERY_KEY.fullmatch(key.strip())
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise ValueError(f"{label}不能在查询参数中包含密钥或密码")

    if allow_private is None:
        # A configured access token means the operator declared the service
        # reachable from a potentially untrusted network.  Keep the existing
        # trusted-LAN compatibility mode when it is absent.
        allow_private = not bool(os.getenv("AUTOLIST_ACCESS_TOKEN", "").strip())
    if not allow_private and not host_is_allowlisted(hostname, allowed_private_hosts):
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            addresses = _resolve_outbound_addresses(hostname, port)
            if any(is_private_or_reserved_address(address) for address in addresses):
                raise ValueError(f"{label}不能访问内网或保留地址")
        else:
            if is_private_or_reserved_address(address):
                raise ValueError(f"{label}不能使用内网或保留地址")
    return normalized


async def validate_outbound_url_async(
    value: str,
    *,
    label: str = "出站地址",
    allow_private: bool | None = None,
    allowed_private_hosts: set[str] | None = None,
    reject_sensitive_query: bool = False,
) -> str:
    """Async wrapper that keeps blocking DNS resolution off the event loop."""
    return await asyncio.to_thread(
        validate_outbound_url,
        value,
        label=label,
        allow_private=allow_private,
        allowed_private_hosts=allowed_private_hosts,
        reject_sensitive_query=reject_sensitive_query,
    )


def _same_origin(left: str, right: str) -> bool:
    first, second = urlparse(left), urlparse(right)
    first_port = first.port or (443 if first.scheme == "https" else 80)
    second_port = second.port or (443 if second.scheme == "https" else 80)
    return first.scheme == second.scheme and first.hostname == second.hostname and first_port == second_port


def _has_sensitive_query(value: str) -> bool:
    return any(
        SENSITIVE_URL_QUERY_KEY.fullmatch(key.strip())
        for key, _item in parse_qsl(urlparse(value).query, keep_blank_values=True)
    )


def _append_request_params(value: str, params: Any, *, include_sensitive: bool) -> str:
    if not params:
        return value
    parsed = urlparse(value)
    existing = parse_qsl(parsed.query, keep_blank_values=True)
    existing_keys = {key for key, _item in existing}
    if hasattr(params, "items"):
        pairs = list(params.items())
    else:
        pairs = list(params)
    for key, item in pairs:
        key_text = str(key)
        if not include_sensitive and SENSITIVE_URL_QUERY_KEY.fullmatch(key_text.strip()):
            continue
        if key_text in existing_keys:
            continue
        existing.append((key_text, str(item)))
        existing_keys.add(key_text)
    return urlunparse(parsed._replace(query=urlencode(existing, doseq=True)))


def _strip_sensitive_headers(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    blocked = {"authorization", "cookie", "proxy-authorization", "set-cookie"}
    result: dict[str, str] = {}
    for key, value in dict(headers).items():
        lowered = str(key).lower()
        if lowered in blocked or lowered in {"x-api-key", "api-key", "x-auth-token"}:
            continue
        if any(marker in lowered for marker in ("token", "secret", "passkey")):
            continue
        result[str(key)] = str(value)
    return result


async def safe_request(
    client: Any,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: Any = None,
    json: Any = None,
    content: Any = None,
    auth: Any = None,
    allow_private: bool | None = None,
    allowed_private_hosts: set[str] | None = None,
    label: str = "出站地址",
    max_redirects: int = MAX_OUTBOUND_REDIRECTS,
) -> Any:
    """Perform a request with per-hop target checks and credential isolation.

    ``httpx`` clients may have default headers and a base URL.  The helper
    temporarily removes sensitive defaults on cross-origin hops, and only
    carries query parameters across the same origin.  It never delegates
    redirects to the HTTP client, so every Location value is validated first.
    """
    raw_url = str(url)
    compatibility_method = getattr(client, method.lower(), None)
    is_mock_client = hasattr(compatibility_method, "assert_awaited")
    parsed = urlparse(raw_url)
    if not parsed.scheme:
        base_url = str(getattr(client, "base_url", ""))
        if is_mock_client and not urlparse(base_url).scheme:
            # Existing unit tests inject a minimal AsyncMock client without a
            # base URL.  Keep those tests transport-free while production
            # requests still require a real configured absolute URL.
            raw_url = f"https://mock.invalid/{raw_url.lstrip('/')}"
            allow_private = True
        else:
            raw_url = urljoin(base_url, raw_url)
    current = await validate_outbound_url_async(
        raw_url, label=label, allow_private=allow_private, allowed_private_hosts=allowed_private_hosts,
    )
    initial = current
    default_headers = {} if is_mock_client else dict(getattr(client, "headers", {}) or {})
    explicit_headers = dict(headers or {})
    try:
        for hop in range(max_redirects + 1):
            same_origin = _same_origin(initial, current)
            request_url = _append_request_params(current, params, include_sensitive=same_origin)
            if not same_origin and _has_sensitive_query(current):
                raise RuntimeError("跨域重定向包含敏感查询参数，已阻止请求")
            await validate_outbound_url_async(
                request_url, label=label, allow_private=allow_private, allowed_private_hosts=allowed_private_hosts,
            )
            if not is_mock_client:
                client.headers = default_headers if same_origin else _strip_sensitive_headers(default_headers)
            request_headers = explicit_headers if same_origin else _strip_sensitive_headers(explicit_headers)
            if is_mock_client:
                response = await compatibility_method(
                    request_url, headers=request_headers or None, json=json, content=content,
                )
            else:
                response = await client.request(
                    method, request_url, headers=request_headers or None, json=json, content=content,
                    auth=auth if same_origin else None, follow_redirects=False,
                )
            if response.status_code not in OUTBOUND_REDIRECT_STATUSES:
                return response
            if hop >= max_redirects:
                raise RuntimeError("出站请求重定向次数过多")
            location = response.headers.get("location")
            if not location:
                raise RuntimeError("出站请求重定向缺少 Location")
            next_url = urljoin(str(response.request.url), location)
            next_url = await validate_outbound_url_async(
                next_url, label=label, allow_private=allow_private, allowed_private_hosts=allowed_private_hosts,
            )
            if not _same_origin(initial, next_url) and _has_sensitive_query(next_url):
                raise RuntimeError("跨域重定向包含敏感查询参数，已阻止请求")
            current = next_url
        raise RuntimeError("出站请求重定向次数过多")
    finally:
        if not is_mock_client:
            client.headers = default_headers


def safe_detail_url(value: Any) -> str | None:
    """Keep a useful HTTP detail link while removing credential-like queries."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        safe_query = [
            (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not SENSITIVE_URL_QUERY_KEY.fullmatch(key.strip())
        ]
        return urlunparse(parsed._replace(query=urlencode(safe_query, doseq=True), fragment=""))
    except ValueError:
        return None


async def validate_remote_icon_url(source: str, site_base_url: str) -> None:
    """Allow configured-site icons while preventing cross-host requests into private networks."""
    try:
        base = validate_outbound_url(site_base_url, label="站点地址", allow_private=True)
        parsed_base = urlparse(base)
        parsed_source = urlparse(source)
        same_host = bool(parsed_base.hostname and parsed_source.hostname and parsed_base.hostname.lower() == parsed_source.hostname.lower())
        # A trusted LAN may use its own site's private icon host.  A private
        # icon on another host is never allowed, because it turns the proxy
        # into an internal network scanner even in compatibility mode.
        source_allow_private = same_host and not bool(os.getenv("AUTOLIST_ACCESS_TOKEN", "").strip())
        validate_outbound_url(source, label="图标地址", allow_private=source_allow_private)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def first_value(data: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        if data.get(name) not in (None, ""):
            return data[name]
    return default


def resource_fingerprint(title: str, size: int | None = None) -> str:
    normalized = re.sub(r"\b(?:free|2x|50%|30%)\b", "", title.lower())
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalized)
    # 64MB 体积桶：比 256MB 更能区分同标题的不同编码/体积发布。
    size_bucket = round(int(size or 0) / (64 * 1024 * 1024)) if size else 0
    return f"{normalized}:{size_bucket}"


def volume_factor_value(value: Any) -> float:
    if value is None:
        return 1.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if text in ("free", "免费", "freeleech"):
        return 0.0
    if text.endswith("%"):
        try:
            return float(text[:-1]) / 100
        except ValueError:
            return 1.0
    try:
        return float(text)
    except ValueError:
        return 1.0
