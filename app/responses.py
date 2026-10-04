"""接口返回格式。

路由用 ``response_model`` 声明返回格式：FastAPI 按这里的字段校验并输出，未声明的字段不会返回。
前端类型由这些声明生成（``scripts/export_openapi.py`` → ``frontend/openapi.json`` →
``openapi-typescript``），字段改动后要重新导出，否则 ``tests/test_contract.py`` 会失败。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# SQLite 的数值列可能存成整数或小数，前端都当作 number。
Number = int | float

FilmStatus = Literal["unrecognized", "unchecked", "missing", "searching", "candidates", "selected", "downloading", "in_library"]
FilmIssue = Literal["no_eligible", "submit_failed", "context_expired", "organize_failed", "download_stalled"]
TaskKind = Literal["search", "recognition", "library", "automation"]


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------- 片单与影片 ----------


class PlaylistSummary(Model):
    id: int
    name: str
    source_type: str | None
    item_count: int


class Film(Model):
    id: int
    playlist_id: int
    rank_no: int | None
    title: str
    original_title: str
    year: int | None
    tmdb_id: int | None
    imdb_id: str | None
    library_state: str | None
    status: FilmStatus
    status_label: str
    issues: list[FilmIssue]
    issue_labels: list[str]
    transfer: Literal["active", "stalled", "paused", "error", "waiting_library", "unknown"] | None
    poster_url: str | None


class FilmPage(Model):
    items: list[Film]
    total: int
    page: int
    page_size: int
    pages: int
    counts: dict[str, int]
    playlists: list[PlaylistSummary]


class SiteOption(Model):
    id: str
    title: str
    site_name: str | None
    detail_url: str | None
    publish_date: str | None
    seeders: int | None
    size: Number | None
    is_free: bool
    in_selection: int
    context_available: bool


class Candidate(Model):
    id: str
    title: str
    site_name: str | None
    detail_url: str | None
    publish_date: str | None
    size: Number | None
    seeders: int | None
    resolution: str | None
    codec: str | None
    group_name: str | None
    recommendation: str
    recommendation_reason: str | None
    eligibility: str
    in_selection: int
    context_available: bool
    is_free: bool
    volume_factor: Number
    site_count: int
    site_options: list[SiteOption]
    site_selection_reason: str


class ExcludedReason(Model):
    reason: str
    count: int


class FilmHistoryRecord(Model):
    id: int
    title: str
    torrent_name: str
    site_name: str | None
    success: int
    message: str
    created_at: str


class FilmSearchSummary(Model):
    task_id: int
    sites: int
    succeeded: int
    failed: int
    results: int
    finished_at: str | None


class FilmDetail(Film):
    backdrop_url: str | None
    playlist_name: str
    library_checked_at: str | None
    candidates: list[Candidate]
    # 有 1080p 等顺序内分辨率可选时，未显示的 720p 等资源数量。
    hidden_low_resolution: int = 0
    excluded_summary: list[ExcludedReason]
    excluded_count: int
    history: list[FilmHistoryRecord]
    search: FilmSearchSummary | None
    search_site_count: int


class TmdbMatch(Model):
    tmdb_id: int
    title: str
    original_title: str
    year: int | None
    overview: str
    current: bool


class FilmSearchStarted(Model):
    id: int
    status: str
    total: int


# ---------- 藏馆首页 ----------


class ActiveTask(Model):
    kind: TaskKind
    id: int
    status: str
    total: int
    completed: int
    playlist_id: int
    playlist_name: str
    trigger: str | None = None
    stage: str | None = None


class Todo(Model):
    key: Literal[
        "search_missing", "pick", "submit", "unrecognized", "unchecked", "no_eligible", "submit_failed",
        "context_expired", "download_stalled", "organize_failed", "failing_sites", "empty_sites",
    ]
    count: int
    names: list[str] | None = None
    emby_configured: bool | None = None


class HomeData(Model):
    playlists: list[PlaylistSummary]
    playlist_id: int | None
    counts: dict[str, int]
    todos: list[Todo]
    tasks: list[ActiveTask]
    recent: list[Film]
    up_next: list[Film]


# ---------- 挑选台 ----------


class PickItem(Film):
    bucket: Literal["candidates", "selected", "no_eligible", "stalled"]
    candidates: list[Candidate]
    # 有 1080p 等顺序内分辨率可选时，未显示的 720p 等资源数量。
    hidden_low_resolution: int = 0
    excluded_summary: list[ExcludedReason]
    excluded_count: int


class PickSearchProgress(Model):
    """进行中的寻片：搜完的影片会陆续出现在挑选台。"""
    task_id: int
    completed: int
    total: int


class PickPage(Model):
    items: list[PickItem]
    counts: dict[str, int]
    playlists: list[PlaylistSummary]
    searching: PickSearchProgress | None = None


# ---------- 动态 ----------


class TimelineEvent(Model):
    id: str
    at: str | None
    kind: Literal["submit", "search", "recognition", "library", "notice", "system"]
    level: Literal["info", "success", "warning", "error"]
    title: str
    detail: str
    film_id: int | None
    task_id: int | None = None
    task_status: str | None = None


class Timeline(Model):
    items: list[TimelineEvent]


# ---------- 待入馆清单与提交 ----------


class SelectionItem(Model):
    id: str
    title: str
    site_name: str | None
    size: Number | None
    resolution: str | None
    rank_no: int | None
    original_title: str
    chinese_title: str | None
    tmdb_title: str | None
    tmdb_year: int | None
    year: int | None
    context_available: bool


class SelectionToggled(Model):
    candidate_id: str
    in_selection: bool


class SubmissionNote(Model):
    candidate_id: str
    title: str
    reason: str


class ExpiredItem(Model):
    candidate_id: str
    title: str


class SubmitResult(Model):
    submitted: int
    needs_research: int
    expired_items: list[ExpiredItem]
    skipped: list[SubmissionNote]
    blocked_unknown: list[SubmissionNote]
    removed: list[SubmissionNote]
    blocked_site: list[SubmissionNote]


class TransferInfo(Model):
    """Transmission 里未完成种子的下载状况（只读）。"""
    state: Literal["downloading", "queued", "checking", "stalled", "paused", "error"]
    percent: Number
    rate_bps: int
    eta_seconds: int | None
    peers: int
    error: str | None


class HistoryRecord(Model):
    id: int
    title: str
    torrent_name: str
    site_name: str | None
    success: int
    message: str | None
    created_at: str
    lifecycle_status: str
    status_label: str
    status_source: str
    status_reason: str
    next_action: str
    transfer: TransferInfo | None = None


class DownloadItem(Model):
    """Transmission 里的一个种子；能对上提交记录时附上片单影片。"""
    hash: str
    name: str
    media_type: Literal["movie", "tv"] | None
    film_id: int | None
    film_title: str | None
    film_year: int | None
    poster_url: str | None
    site: str | None
    state: Literal["downloading", "checking", "stalled", "error", "queued", "paused", "seeding", "completed"]
    percent: Number
    size: int
    downloaded: int
    rate_down: int
    rate_up: int
    eta_seconds: int | None
    seeders: int
    leechers: int
    ratio: Number | None
    added_at: str | None
    done_at: str | None
    error: str | None


class DownloadSummary(Model):
    total: int
    counts: dict[str, int]
    download_bps: int
    upload_bps: int
    free_bytes: int | None


class DownloadsPage(Model):
    configured: bool
    error: str | None
    items: list[DownloadItem]
    summary: DownloadSummary | None
    web_url: str | None
    checked_at: str


class SyncSource(Model):
    """一个外部系统在最近一次同步时的情况。"""
    configured: bool
    ok: bool
    checked_at: str
    message: str | None


class SyncResult(Model):
    ran_at: str
    duration_ms: int
    sources: dict[str, SyncSource]
    downloading: int
    arrived: int
    removed: int


class SyncEventStat(Model):
    count: int
    last_at: str | None
    last_kind: str | None


class SyncEventRecord(Model):
    at: str
    source: str
    kind: str
    hash: str | None
    tmdb_id: int | None
    fields: list[str] | None


class SyncStatus(Model):
    running: bool
    last: SyncResult | None
    interval_seconds: int
    events: dict[str, SyncEventStat]
    recent: list[SyncEventRecord]


class TransmissionHooks(Model):
    """Transmission 当前启用的“添加”“完成”脚本（空字符串表示未启用）。"""
    added: str
    done: str


class SyncWebhooks(Model):
    paths: dict[str, str]
    transmission_hooks: TransmissionHooks | None


class SyncEventAccepted(Model):
    accepted: bool


class HistoryCleared(Model):
    deleted: int
    status: str


# ---------- 寻片任务 ----------


class AttemptSummary(Model):
    total: int
    succeeded: int | None
    failed: int | None


class SearchTask(Model):
    id: int
    playlist_id: int
    range_start: int
    range_end: int
    status: str
    total: int
    completed: int
    matched: int
    trigger: str
    parent_task_id: int | None
    error_message: str | None
    created_at: str
    updated_at: str
    attempt_summary: AttemptSummary


class SearchAttempt(Model):
    id: int
    playlist_item_id: int
    rank_no: int | None
    original_title: str
    site_id: int | None
    site_name: str
    attempt_no: int
    status: str
    result_count: int
    duration_ms: int
    error_code: str | None
    error_message: str | None
    finished_at: str


class SiteAttemptSummary(Model):
    site_id: int | None
    site_name: str
    total: int
    succeeded: int | None
    failed: int | None
    average_ms: int | None
    verify_url: str | None = None


class SearchAttempts(Model):
    items: list[SearchAttempt]
    sites: list[SiteAttemptSummary]


class SearchTaskLog(Model):
    id: int
    level: str
    stage: str
    message: str
    created_at: str


class SearchTaskStarted(Model):
    id: int
    status: str
    total: int
    scope: str | None = None
    parent_task_id: int | None = None


class SearchTaskCancelled(Model):
    id: int
    status: str


# ---------- 站点 ----------


class SiteLocalStats(Model):
    total: int
    succeeded: int
    success_rate: Number | None
    average_ms: int | None
    result_count: int
    last_attempt_at: str | None


class SiteAccountStats(Model):
    uploaded: Number | None
    downloaded: Number | None
    ratio: Number | None
    bonus: Number | None
    seeding: Number | None
    checked_at: str | None
    error: str | None


class Site(Model):
    id: int
    name: str
    adapter: str
    base_url: str
    api_key: str
    cookie: str
    user_agent: str
    priority: int
    timeout_seconds: int
    rss_url: str
    icon_url: str
    proxy: int
    render: int
    limit_interval: int | None
    limit_count: int | None
    enabled: int
    search_enabled: int
    supplement_only: int
    migration_note: str | None
    last_status: str
    last_message: str | None
    last_duration_ms: int | None
    last_tested_at: str | None
    verify_url: str | None
    cookie_updated_at: str | None
    cookie_source: Literal["cookiecloud", "manual", "moviepilot"] | None
    profile: str
    api_key_configured: bool
    cookie_configured: bool
    user_agent_configured: bool
    rss_url_configured: bool
    icon_endpoint: str
    local_stats: SiteLocalStats
    account_stats: SiteAccountStats


class SiteSaved(Model):
    id: int
    name: str
    adapter: str
    enabled: bool
    search_enabled: bool


class SiteCheck(Model):
    id: int
    name: str
    ok: bool
    status: str
    duration_ms: int
    message: str


class SiteChecks(Model):
    total: int
    ok: int
    results: list[SiteCheck]


class MoviePilotSitesSynced(Model):
    ok: bool
    total: int
    sites: list[str]
    skipped: list[str] = []
    cookiecloud_updated: int
    message: str


class CookieCloudSynced(Model):
    ok: bool
    updated: int
    unchanged: int
    sites: list[str]
    missing: list[str]
    message: str


class SiteCookieRefreshed(Model):
    ok: bool
    id: int
    name: str
    message: str


class CookieSync(Model):
    at: str
    origin: str
    updated: list[str]
    unchanged: int
    missing: list[str]


class CookieCloudStatus(Model):
    configured: bool
    key_valid: bool
    url_configured: bool
    url: str
    pull_interval_minutes: int
    last_sync: CookieSync | None


# ---------- 片单管理 ----------


class PlaylistRow(Model):
    id: int
    name: str
    position: int
    source_type: str | None
    source_url: str | None
    source_name: str | None
    last_synced_at: str | None
    created_at: str
    item_count: int
    recognized_count: int | None
    sync_enabled: int
    sync_interval_hours: int
    next_sync_at: str | None
    last_sync_status: str | None
    last_sync_message: str | None
    automation_enabled: int
    automation_batch_size: int


class ImportSample(Model):
    rank_no: int
    original_title: str
    chinese_title: str | None = None
    year: int | None = None


class ImportPreview(Model):
    name: str
    count: int
    source_type: str | None
    sample: list[ImportSample]


class PlaylistImported(Model):
    id: int
    name: str
    count: int
    recognition_task_id: int | None
    recognition_note: str | None


class PlaylistSynced(Model):
    id: int
    added: int
    message: str
    trigger: str


class AutomationStarted(Model):
    id: int
    status: str
    message: str


# ---------- 设置与连接 ----------


class ProviderCheck(Model):
    # 各服务的检测结果另带版本、服务器名等说明字段，原样返回。
    model_config = ConfigDict(extra="allow")

    ok: bool | None = None
    configured: bool | None = None
    message: str | None = None


class ConnectionStatus(Model):
    ok: bool
    message: str
    version: str
    providers: dict[str, ProviderCheck]


class PublicSettings(Model):
    """运行设置：密钥只返回是否已配置（字段本身为空或掩码）。"""

    mp_base_url: str
    mp_api_key: str
    mp_api_key_configured: bool
    mp_timeout_seconds: Number
    emby_base_url: str
    emby_api_key: str
    emby_api_key_configured: bool
    tmdb_api_key: str
    tmdb_api_key_configured: bool
    tmdb_language: str
    fanart_api_key: str
    fanart_api_key_configured: bool
    mdblist_api_key: str
    mdblist_api_key_configured: bool
    cookiecloud_url: str
    cookiecloud_url_configured: bool
    cookiecloud_key: str
    cookiecloud_key_configured: bool
    cookiecloud_password: str
    cookiecloud_password_configured: bool
    outbound_proxy_configured: bool
    outbound_proxy_url_configured: bool
    outbound_proxy_url: str
    pt_proxy_enabled: bool
    tmdb_proxy_enabled: bool
    ai_base_url: str
    ai_api_key: str
    ai_api_key_configured: bool
    ai_model: str
    tr_base_url: str
    tr_username: str
    tr_username_configured: bool
    tr_password: str
    tr_password_configured: bool
    dashboard_random_posters: bool
    access_token_required: bool
    access_token_strength: Literal["missing", "weak", "strong"]
    access_token_strength_enforced: bool


class ScoreLine(Model):
    label: str
    score: int


class CandidateAnalysis(Model):
    ranking: int
    score: int
    breakdown: list[ScoreLine]
    tier: int
    group: str | None
    resolution: str
    codec: str
    source: str
    recommendation: str
    reason: str
    manual: bool
    manual_reasons: list[str]
    eligible: bool
    exclusion_reason: str | None
    profile_id: str | None
    profile_label: str | None


class ReleaseGroupCatalog(Model):
    builtin_count: int
    custom_count: int
    merged_count: int
    builtin_names: list[str]
    custom_rules: list[str]


class LogEvent(Model):
    # 结构化日志的其余字段因事件而异，原样返回。
    model_config = ConfigDict(extra="allow")

    ts: str
    level: str
    event: str
