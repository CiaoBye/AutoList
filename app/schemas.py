"""HTTP request payload models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ImportPayload(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    json_data: dict[str, Any] | list[dict[str, Any]] | None = None
    xlsx_base64: str | None = Field(default=None, max_length=50_000_000)
    csv_text: str | None = Field(default=None, max_length=20_000_000)
    source_url: str | None = Field(default=None, max_length=2048)
    limit: int = Field(default=5000, ge=1, le=10000)


class TaskPayload(BaseModel):
    playlist_id: int
    scope: str = Field(default="range", pattern="^(range|pending)$")
    count: int = Field(default=50, ge=1, le=10000)
    range_start: int = Field(default=1, ge=1)
    range_end: int = Field(default=1, ge=1)


class ConfigPayload(BaseModel):
    preferred_resolutions: str | None = None
    minimum_resolution: str | None = None
    preferred_codecs: str | None = None
    priority_groups: str | None = None
    secondary_groups: str | None = None
    fallback_groups: str | None = None
    allow_unknown_groups: bool | None = None
    candidate_limit: int | None = Field(default=None, ge=1, le=20)
    scoring_policy: dict[str, Any] | None = None
    candidate_policy: dict[str, Any] | None = None


class ScorePreviewPayload(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    seeders: int = Field(default=0, ge=0)
    volume_factor: float = Field(default=1, ge=0)
    scoring_policy: dict[str, Any] | None = None
    candidate_policy: dict[str, Any] | None = None


class RuntimeSettingsPayload(BaseModel):
    mp_base_url: str = ""
    mp_api_key: str | None = None
    mp_timeout_seconds: float = Field(default=30, ge=3, le=300)
    emby_base_url: str = ""
    emby_api_key: str | None = None
    tmdb_api_key: str | None = None
    tmdb_language: str = "zh-CN"
    mdblist_api_key: str | None = None
    cookiecloud_key: str | None = Field(default=None, max_length=128, pattern=r"^(?:[A-Za-z0-9_-]{5,128})?$")
    cookiecloud_password: str | None = None
    outbound_proxy_url: str | None = None
    tmdb_proxy_enabled: bool = False
    pt_proxy_enabled: bool = False
    ai_base_url: str = ""
    ai_api_key: str | None = None
    ai_model: str = ""
    tr_base_url: str = ""
    tr_username: str = ""
    tr_password: str | None = None
    dashboard_random_posters: bool = False


class SitePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = ""
    api_key: str | None = None
    cookie: str | None = None
    user_agent: str = ""
    priority: int = Field(default=100, ge=1, le=999)
    timeout_seconds: int = Field(default=30, ge=3, le=300)
    rss_url: str = ""
    icon_url: str = ""
    proxy: bool = False
    render: bool = False
    limit_interval: int | None = Field(default=None, ge=1)
    limit_count: int | None = Field(default=None, ge=1)
    enabled: bool = True
    search_enabled: bool = False
    clear_api_key: bool = False
    clear_cookie: bool = False
    clear_rss_url: bool = False


class CookieCloudUploadPayload(BaseModel):
    uuid: str = Field(min_length=5, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    encrypted: str = Field(min_length=16, max_length=32_000_000)
    crypto_type: str = Field(default="legacy", pattern=r"^(legacy|aes-128-cbc-fixed)$")


class PlaylistUpdatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class PlaylistOrderPayload(BaseModel):
    ids: list[int]


class PlaylistAutomationPayload(BaseModel):
    enabled: bool = False
    auto_cart: bool = False
    batch_size: int = Field(default=50, ge=1, le=200)


class PlaylistSyncPayload(BaseModel):
    enabled: bool = False
    interval_hours: int = Field(default=24, ge=1, le=720)
