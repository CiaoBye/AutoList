"""Shared helpers used by routes and services."""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import re
import socket
import sqlite3
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

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


async def validate_remote_icon_url(source: str, site_base_url: str) -> None:
    """Allow configured-site icons while preventing cross-host requests into private networks."""
    parsed = urlparse(source)
    base = urlparse(site_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("图标地址无效")
    if parsed.hostname.lower() == (base.hostname or "").lower():
        return
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise RuntimeError("图标域名无法解析") from exc
    if not addresses or any(is_private_or_reserved_address(ipaddress.ip_address(address[4][0])) for address in addresses):
        raise RuntimeError("图标地址不允许访问内网")


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
