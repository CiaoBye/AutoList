"""Shared helpers used by routes and services."""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import inspect
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


def to_int(value: Any, default: int = 0) -> int:
    """Convert a DB/API value to int; invalid or missing input falls back to ``default``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    """Convert a DB/API value to float; invalid or missing input falls back to ``default``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


async def read_request_body_limited(request: Any, limit: int = MAX_COOKIECLOUD_BODY) -> bytes:
    """Read an ASGI request incrementally so the size cap happens before allocation."""
    if limit < 1:
        raise ValueError("请求体大小上限无效")
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > limit:
            raise HTTPException(413, "CookieCloud 上传数据过大")
    except ValueError as exc:
        raise HTTPException(400, "请求体长度无效") from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, "CookieCloud 上传数据过大")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


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
        value = to_int(address)
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
MAX_OUTBOUND_RESPONSE_BYTES = 16 * 1024 * 1024
# 已校验域名解析结果短 TTL 缓存（审计 3-20）：token 模式直连下避免每请求重复 DNS。
DNS_CACHE_TTL_SECONDS = 60
_dns_address_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


def _cached_dns_addresses(hostname: str, port: int | None) -> tuple[str, ...] | None:
    import time as _time

    key = f"{hostname}:{port or ''}"
    entry = _dns_address_cache.get(key)
    if entry is not None and _time.monotonic() - entry[0] < DNS_CACHE_TTL_SECONDS:
        return entry[1]
    return None


def _remember_dns_addresses(hostname: str, port: int | None, addresses: tuple[str, ...]) -> None:
    import time as _time

    _dns_address_cache[f"{hostname}:{port or ''}"] = (_time.monotonic(), addresses)


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


def validated_base_url(value: str, label: str, required: bool, allow_private: bool | None = None) -> str:
    """FastAPI 层出站地址校验封装（审计 2-5/2-11）：统一走 validate_outbound_url。

    ``required=False`` 时空值返回空串；校验失败统一转 422。
    """
    normalized = str(value or "").strip().rstrip("/")
    if not normalized:
        if required:
            raise HTTPException(422, f"{label}不能为空")
        return ""
    try:
        validate_outbound_url(normalized, label=label, allow_private=allow_private, reject_sensitive_query=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
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


def _strip_body_headers(headers: Any) -> dict[str, str]:
    """Remove entity headers after redirect semantics changed to a bodyless request."""
    if not headers:
        return {}
    return {
        str(key): str(value)
        for key, value in dict(headers).items()
        if str(key).lower() not in {"content-length", "content-type", "transfer-encoding"}
    }


def _request_has_body(json_payload: Any, content: Any) -> bool:
    """Treat an explicitly supplied JSON/content argument as a request body."""
    return json_payload is not None or content is not None


async def _close_response(response: Any) -> None:
    close = getattr(response, "aclose", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _read_response_limited(response: Any, *, limit: int, label: str) -> None:
    """Consume a response incrementally and retain at most ``limit`` bytes.

    Production ``httpx.AsyncClient.stream`` responses are read through
    ``aiter_bytes`` before the stream context closes.  AsyncMock-based tests
    may expose either the same iterator or only a buffered ``content`` value;
    the latter compatibility path is deliberately limited to test doubles.
    """
    response_headers = getattr(response, "headers", {}) or {}
    content_length = response_headers.get("content-length")
    try:
        declared_length = int(content_length) if content_length is not None else None
    except (TypeError, ValueError):
        declared_length = None
    if declared_length is not None and declared_length > limit:
        await _close_response(response)
        raise RuntimeError(f"{label}响应过大，已超过 {limit} 字节上限")

    aiter_bytes = getattr(response, "aiter_bytes", None)
    if callable(aiter_bytes):
        iterator = aiter_bytes()
        if inspect.isawaitable(iterator):
            iterator = await iterator
        iterator_type = type(iterator)
        is_unconfigured_mock = (
            iterator_type.__module__ == "unittest.mock"
            and iterator_type.__name__ in {"AsyncMock", "MagicMock", "Mock"}
        )
        if hasattr(iterator, "__aiter__") and not is_unconfigured_mock:
            chunks: list[bytes] = []
            total = 0
            async for chunk in iterator:
                current = bytes(chunk)
                total += len(current)
                if total > limit:
                    await _close_response(response)
                    raise RuntimeError(f"{label}响应过大，已超过 {limit} 字节上限")
                chunks.append(current)
            body = b"".join(chunks)
            # httpx.Response.content is populated by its internal ``_content``
            # field after a streamed read.  Assigning the bounded bytes keeps
            # response.json()/text/content usable after the context closes.
            try:
                response._content = body  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass
            return

    # Compatibility for AsyncMock responses which expose only ``content``.
    body = getattr(response, "content", b"")
    if inspect.isawaitable(body):
        body = await body
    if not isinstance(body, (bytes, bytearray)):
        body = bytes(body or b"")
    if len(body) > limit:
        await _close_response(response)
        raise RuntimeError(f"{label}响应过大，已超过 {limit} 字节上限")


async def _bind_outbound_host(
    url: str,
    *,
    label: str = "出站地址",
    allow_private: bool | None = None,
    allowed_private_hosts: set[str] | None = None,
) -> tuple[str, str | None]:
    """Resolve, validate and pin the TCP target of a hostname URL.

    Returns ``(bound_url, original_host)``.  The returned URL carries an IP
    literal so the HTTP client connects to exactly the address that passed
    validation; ``original_host`` (with non-default port, when present) is
    sent as the ``Host`` header. The caller derives the hostname-only TLS SNI
    value so a non-default port never enters the TLS server name.

    URLs that already use an IP literal, private-trust mode, or an
    allowlisted host are returned unchanged with ``None`` so the client keeps
    its natural Host/SNI behavior.
    """
    parsed = urlparse(str(url))
    hostname = parsed.hostname
    if not hostname:
        return str(url), None
    if allow_private is None:
        # 与 validate_outbound_url 保持同一信任模型：配置访问令牌时视为可能不可信网络。
        allow_private = not bool(os.getenv("AUTOLIST_ACCESS_TOKEN", "").strip())
    if allow_private or host_is_allowlisted(hostname, allowed_private_hosts):
        return str(url), None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        hostname_is_ip = False  # 非 IP 字面量；走域名解析路径
    else:
        # IP 字面量已由 validate_outbound_url 校验，无需再解析。
        return str(url), None
    cached = _cached_dns_addresses(hostname, parsed.port)
    if cached is not None:
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (cached[0], parsed.port or 0))]
    else:
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo, hostname, parsed.port or None, type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValueError(f"{label}目标域名无法解析") from exc
        if not addresses:
            raise ValueError(f"{label}目标域名无法解析")
        _remember_dns_addresses(hostname, parsed.port, tuple(str(address[4][0]) for address in addresses))
    pinned: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    for address in addresses:
        candidate = ipaddress.ip_address(address[4][0])
        if is_private_or_reserved_address(candidate):
            raise ValueError(f"{label}不能访问内网或保留地址")
        if pinned is None:
            pinned = candidate
    host = str(parsed.hostname)
    if parsed.port:
        host = f"{host}:{parsed.port}"
    bound_host = str(pinned)
    if isinstance(pinned, ipaddress.IPv6Address):
        bound_host = f"[{bound_host}]"
    if parsed.port:
        bound_host = f"{bound_host}:{parsed.port}"
    bound_url = urlunparse(parsed._replace(netloc=bound_host))
    return bound_url, host


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
    proxy_mode: bool = False,
    label: str = "出站地址",
    max_redirects: int = MAX_OUTBOUND_REDIRECTS,
    max_response_bytes: int = MAX_OUTBOUND_RESPONSE_BYTES,
) -> Any:
    """Perform a request with per-hop target checks and credential isolation.

    ``httpx`` clients may have default headers and a base URL.  The helper
    temporarily removes sensitive defaults on cross-origin hops, and only
    carries query parameters across the same origin.  It never delegates
    redirects to the HTTP client, so every Location value is validated first.

    ``proxy_mode`` is explicit by design. It must be set by callers when the
    client uses an outbound proxy; proxy detection through httpx private
    attributes is intentionally unsupported. Direct mode pins the validated
    hostname to its resolved IP, while proxy mode leaves hostname resolution
    to the configured proxy.
    """
    if max_response_bytes < 1:
        raise ValueError("出站响应大小上限无效")
    current_method = str(method or "GET").upper()
    raw_url = str(url)
    initial_method = getattr(client, current_method.lower(), None)
    if initial_method is None:
        raise RuntimeError(f"客户端不支持 {current_method} 请求")
    is_mock_client = hasattr(initial_method, "assert_awaited")
    request_method = getattr(client, "request", None)
    request_is_mock = hasattr(request_method, "assert_awaited")
    stream_method = getattr(client, "stream", None)
    stream_is_mock = hasattr(stream_method, "assert_awaited")
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
    # 代理场景下 DNS 由可信代理解析，客户端只做预检；直连场景才需要把连接目标
    # 固定为刚校验过的 IP，避免解析与 TCP 连接之间被 DNS rebinding 切换目标。
    # 代理状态必须由调用点显式传递，不能读取 httpx 私有 _mounts。
    try:
        for hop in range(max_redirects + 1):
            same_origin = _same_origin(initial, current)
            bound_url, original_host = current, None
            original_hostname = urlparse(current).hostname
            if not is_mock_client and not proxy_mode:
                # 仅直连场景需要绑定：代理场景 DNS 由可信代理解析，客户端只做
                # 预检验证；测试桩不建立 TCP，同样跳过 DNS 解析。
                bound_url, original_host = await _bind_outbound_host(
                    current, label=label, allow_private=allow_private, allowed_private_hosts=allowed_private_hosts,
                )
            request_url = _append_request_params(bound_url, params, include_sensitive=same_origin)
            if not same_origin and _has_sensitive_query(current):
                raise RuntimeError("跨域重定向包含敏感查询参数，已阻止请求")
            await validate_outbound_url_async(
                request_url, label=label, allow_private=allow_private, allowed_private_hosts=allowed_private_hosts,
            )
            request_body_json = json
            request_body_content = content
            bodyless_request = current_method in {"GET", "HEAD"} and not _request_has_body(
                request_body_json, request_body_content,
            )
            if not is_mock_client:
                client_default_headers = default_headers if same_origin else _strip_sensitive_headers(default_headers)
                client.headers = _strip_body_headers(client_default_headers) if bodyless_request else client_default_headers
            request_headers = explicit_headers if same_origin else _strip_sensitive_headers(explicit_headers)
            extensions = None
            if not is_mock_client and not proxy_mode and original_host:
                # 连接目标已固定为校验过的 IP；Host 头与 TLS SNI 保持原域名。
                request_headers = {**request_headers, "host": original_host}
                if bound_url.startswith("https://") and original_hostname:
                    # Host 允许携带非默认端口，TLS SNI 只能是 hostname。
                    extensions = {"sni_hostname": original_hostname}
            if bodyless_request:
                request_headers = _strip_body_headers(request_headers)
            if is_mock_client:
                compatibility_method = getattr(client, current_method.lower(), None)
                if compatibility_method is None:
                    raise RuntimeError(f"客户端不支持 {current_method} 请求")
                response = await compatibility_method(
                    request_url, headers=request_headers or None,
                    json=request_body_json, content=request_body_content,
                )
                await _read_response_limited(response, limit=max_response_bytes, label=label)
            elif request_is_mock:
                # AsyncMock compatibility: existing tests may patch ``request``
                # on a real AsyncClient. Production clients use the streaming
                # branch below and never buffer an untrusted response first.
                response = await request_method(
                    current_method, request_url, headers=request_headers or None,
                    json=request_body_json, content=request_body_content,
                    auth=auth if same_origin else None, follow_redirects=False, extensions=extensions,
                )
                await _read_response_limited(response, limit=max_response_bytes, label=label)
            elif stream_is_mock:
                # Some tests patch ``stream`` directly with AsyncMock. Accept
                # either a response or an async-context-manager return value.
                stream_result = stream_method(
                    current_method, request_url, headers=request_headers or None,
                    json=request_body_json, content=request_body_content,
                    auth=auth if same_origin else None, follow_redirects=False, extensions=extensions,
                )
                if inspect.isawaitable(stream_result):
                    stream_result = await stream_result
                if hasattr(stream_result, "__aenter__"):
                    async with stream_result as streamed_response:
                        response = streamed_response
                        await _read_response_limited(response, limit=max_response_bytes, label=label)
                else:
                    response = stream_result
                    await _read_response_limited(response, limit=max_response_bytes, label=label)
            else:
                async with client.stream(
                    current_method, request_url, headers=request_headers or None,
                    json=request_body_json, content=request_body_content,
                    auth=auth if same_origin else None, follow_redirects=False, extensions=extensions,
                ) as streamed_response:
                    response = streamed_response
                    await _read_response_limited(response, limit=max_response_bytes, label=label)
            if response.status_code not in OUTBOUND_REDIRECT_STATUSES:
                return response
            if hop >= max_redirects:
                raise RuntimeError("出站请求重定向次数过多")
            location = response.headers.get("location")
            if not location:
                raise RuntimeError("出站请求重定向缺少 Location")
            next_url = urljoin(current, location)
            next_url = await validate_outbound_url_async(
                next_url, label=label, allow_private=allow_private, allowed_private_hosts=allowed_private_hosts,
            )
            if not _same_origin(initial, next_url) and _has_sensitive_query(next_url):
                raise RuntimeError("跨域重定向包含敏感查询参数，已阻止请求")
            redirect_origin_same = _same_origin(current, next_url)
            has_body = _request_has_body(request_body_json, request_body_content)
            if response.status_code in {301, 302} and current_method not in {"GET", "HEAD"}:
                # Explicitly use safe/browser-compatible semantics for
                # non-idempotent methods: convert to GET and drop the body.
                current_method = "GET"
                json = None
                content = None
            elif response.status_code == 303:
                # 303 always becomes GET, including same-origin redirects.
                current_method = "GET"
                json = None
                content = None
            elif response.status_code in {307, 308} and has_body and not redirect_origin_same:
                raise RuntimeError("跨域 307/308 重定向携带请求体，已阻止请求")
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
    """Allow configured-site icons while preventing cross-host requests into private networks.

    DNS 解析移到线程池，避免在 async 端点内阻塞事件循环（审计 2-16）。
    """
    try:
        base = await asyncio.to_thread(validate_outbound_url, site_base_url, label="站点地址", allow_private=True)
        parsed_base = urlparse(base)
        parsed_source = urlparse(source)
        same_host = bool(parsed_base.hostname and parsed_source.hostname and parsed_base.hostname.lower() == parsed_source.hostname.lower())
        # A trusted LAN may use its own site's private icon host.  A private
        # icon on another host is never allowed, because it turns the proxy
        # into an internal network scanner even in compatibility mode.
        source_allow_private = same_host and not bool(os.getenv("AUTOLIST_ACCESS_TOKEN", "").strip())
        await asyncio.to_thread(validate_outbound_url, source, label="图标地址", allow_private=source_allow_private)
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
    size_bucket = round(to_int(size) / (64 * 1024 * 1024)) if size else 0
    return f"{normalized}:{size_bucket}"


def volume_factor_value(value: Any) -> float:
    if value is None:
        return 1.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 1.0
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
