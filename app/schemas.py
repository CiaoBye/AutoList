"""HTTP request payload models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


MAX_SEARCH_ITEMS = 2000
MAX_SEARCH_RANGE_END = 200_000
MAX_IMPORT_JSON_BYTES = 10 * 1024 * 1024
MAX_IMPORT_ROWS = 200_000
# CookieCloud 用户 KEY 的唯一格式定义：设置保存、扩展上传、读取与本地存储共用。
COOKIECLOUD_KEY_PATTERN = r"[A-Za-z0-9_-]{5,128}"


class ImportPayload(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    json_data: dict[str, Any] | list[dict[str, Any]] | None = None
    xlsx_base64: str | None = Field(default=None, max_length=50_000_000)
    csv_text: str | None = Field(default=None, max_length=20_000_000)
    source_url: str | None = Field(default=None, max_length=2048)
    limit: int = Field(default=5000, ge=1, le=10000)

    @field_validator("json_data")
    @classmethod
    def validate_json_data_limits(cls, value: dict[str, Any] | list[dict[str, Any]] | None) -> dict[str, Any] | list[dict[str, Any]] | None:
        if value is None:
            return None
        import json

        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_IMPORT_JSON_BYTES:
            raise ValueError("JSON 片单数据过大")
        entries = value if isinstance(value, list) else value.get("films") or value.get("items") or []
        if isinstance(entries, list) and len(entries) > MAX_IMPORT_ROWS:
            raise ValueError(f"JSON 条目数超过上限（{MAX_IMPORT_ROWS} 条）")
        return value


class TaskPayload(BaseModel):
    playlist_id: int
    scope: str = Field(default="range", pattern="^(range|pending)$")
    count: int = Field(default=50, ge=1, le=MAX_SEARCH_ITEMS)
    range_start: int = Field(default=1, ge=1, le=MAX_SEARCH_RANGE_END)
    range_end: int = Field(default=1, ge=1, le=MAX_SEARCH_RANGE_END)

    @model_validator(mode="after")
    def validate_search_range(self) -> "TaskPayload":
        if self.scope == "range" and self.range_end - self.range_start + 1 > MAX_SEARCH_ITEMS:
            raise ValueError(f"单次搜索最多处理 {MAX_SEARCH_ITEMS} 部影片")
        return self


class ConfigPayload(BaseModel):
    # 旧版评分键已废弃（审计 2-7）：candidate_policy 是唯一评分配置源。
    candidate_limit: int | None = Field(default=None, ge=1, le=20)
    candidate_policy: dict[str, Any] | None = None


class ScorePreviewPayload(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    seeders: int = Field(default=0, ge=0)
    volume_factor: float = Field(default=1, ge=0)
    candidate_policy: dict[str, Any] | None = None


class RuntimeSettingsPayload(BaseModel):
    mp_base_url: str = Field(default="", max_length=2048)
    mp_api_key: str | None = Field(default=None, max_length=512)
    mp_timeout_seconds: float = Field(default=30, ge=3, le=300)
    emby_base_url: str = Field(default="", max_length=2048)
    emby_api_key: str | None = Field(default=None, max_length=512)
    tmdb_api_key: str | None = Field(default=None, max_length=512)
    tmdb_language: str = Field(default="zh-CN", max_length=32)
    fanart_api_key: str | None = Field(default=None, max_length=512)
    mdblist_api_key: str | None = Field(default=None, max_length=512)
    cookiecloud_url: str | None = Field(default=None, max_length=2048)
    cookiecloud_key: str | None = Field(default=None, max_length=128, pattern=rf"^(?:{COOKIECLOUD_KEY_PATTERN})?$")
    cookiecloud_password: str | None = Field(default=None, max_length=2048)
    outbound_proxy_url: str | None = Field(default=None, max_length=2048)
    tmdb_proxy_enabled: bool = False
    pt_proxy_enabled: bool = False
    ai_base_url: str = Field(default="", max_length=2048)
    ai_api_key: str | None = Field(default=None, max_length=512)
    ai_model: str = Field(default="", max_length=120)
    tr_base_url: str = Field(default="", max_length=2048)
    tr_username: str | None = Field(default=None, max_length=120)
    tr_password: str | None = Field(default=None, max_length=2048)
    dashboard_random_posters: bool = False


class SitePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(default="", max_length=2048)
    api_key: str | None = Field(default=None, max_length=512)
    cookie: str | None = Field(default=None, max_length=65536)
    user_agent: str = Field(default="", max_length=512)
    priority: int = Field(default=100, ge=1, le=999)
    timeout_seconds: int = Field(default=30, ge=3, le=300)
    rss_url: str = Field(default="", max_length=2048)
    icon_url: str = Field(default="", max_length=1_500_000)
    proxy: bool = True
    render: bool = False
    limit_interval: int | None = Field(default=None, ge=1)
    limit_count: int | None = Field(default=None, ge=1)
    enabled: bool = True
    search_enabled: bool = False
    supplement_only: bool = False
    clear_api_key: bool = False
    clear_cookie: bool = False
    clear_rss_url: bool = False




class PlaylistUpdatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class PlaylistOrderPayload(BaseModel):
    ids: list[int]


class PlaylistAutomationPayload(BaseModel):
    enabled: bool = False
    auto_select: bool = False
    batch_size: int = Field(default=50, ge=1, le=200)


class PlaylistSyncPayload(BaseModel):
    enabled: bool = False
    interval_hours: int = Field(default=24, ge=1, le=720)


class FilmTmdbPayload(BaseModel):
    """Manually pin a film to a TMDB movie id when automatic recognition picked the wrong one."""

    tmdb_id: int = Field(ge=1, le=100_000_000)
