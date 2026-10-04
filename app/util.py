"""Shared helpers used by routes and services."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any


from .security import sanitize_sensitive_text


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


# 种子名里的剧集标记：S01、S01-S05、S01E02、Season 2、第二季、全 10 集。
_SERIES_NAME = re.compile(
    r"(?<![A-Za-z0-9])(?:S\d{1,2}(?:[-~.]?(?:E\d{1,3}|S?\d{1,2}))?|Season[ ._-]?\d+|E\d{2,3}[-~]E?\d{2,3})(?![A-Za-z0-9])|第[一二三四五六七八九十\d]{1,3}季|全\d{1,3}集",
    re.I,
)


def media_kind(value: Any) -> str | None:
    """MoviePilot 给的媒体类型（电影 / 电视剧，或 movie / tv）归一为 ``movie`` / ``tv``，认不出返回 None。"""
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if "电视剧" in text or text.endswith("tv") or text in {"tv", "series", "show"}:
        return "tv"
    if "电影" in text or "movie" in text:
        return "movie"
    return None


def looks_like_series(name: Any) -> bool:
    """种子名带剧集标记（S01、第二季……）时多半是剧集。"""
    return bool(_SERIES_NAME.search(str(name or "")))


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


def json_ids(raw: Any) -> list[int] | None:
    """解析任务快照里的影片编号列表；格式不对时返回 None。"""
    try:
        values = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return [to_int(value) for value in values] if isinstance(values, list) else None
