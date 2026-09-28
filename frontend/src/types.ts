export type FilmStatus =
  | "unrecognized"
  | "unchecked"
  | "missing"
  | "searching"
  | "candidates"
  | "selected"
  | "downloading"
  | "in_library";

export type FilmIssue = "no_eligible" | "submit_failed" | "context_expired";

export interface Film {
  id: number;
  playlist_id: number;
  rank_no: number | null;
  title: string;
  original_title: string;
  year: number | null;
  tmdb_id: number | null;
  imdb_id: string | null;
  library_state: string | null;
  status: FilmStatus;
  status_label: string;
  issues: FilmIssue[];
  issue_labels: string[];
  transfer: "active" | "waiting_library" | "unknown" | null;
  poster_url: string | null;
}

export interface Playlist {
  id: number;
  name: string;
  source_type: string | null;
  item_count: number;
}

export type Counts = Record<FilmStatus | "all" | `issue:${FilmIssue}`, number>;

export interface FilmPage {
  items: Film[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
  counts: Counts;
  playlists: Playlist[];
}

export interface SiteOption {
  id: string;
  site_name: string | null;
  seeders: number | null;
  size: number | null;
  is_free: boolean;
  in_cart: number;
  context_available: boolean;
}

export interface Candidate {
  id: string;
  title: string;
  site_name: string | null;
  size: number | null;
  seeders: number | null;
  resolution: string | null;
  codec: string | null;
  group_name: string | null;
  recommendation: "preferred" | "fallback" | "manual" | "excluded" | string;
  recommendation_reason: string | null;
  eligibility: string;
  in_cart: number;
  context_available: boolean;
  is_free: boolean;
  volume_factor: number;
  site_count: number;
  site_options: SiteOption[];
  site_selection_reason: string;
}

export interface HistoryRecord {
  id: number;
  title: string;
  torrent_name: string;
  site_name: string | null;
  success: number;
  message: string;
  created_at: string;
}

export interface SearchSummary {
  task_id: number;
  sites: number;
  succeeded: number;
  failed: number;
  results: number;
  finished_at: string | null;
}

export interface FilmDetail extends Film {
  backdrop_url: string | null;
  playlist_name: string;
  library_checked_at: string | null;
  candidates: Candidate[];
  excluded_summary: { reason: string; count: number }[];
  excluded_count: number;
  history: HistoryRecord[];
  search: SearchSummary | null;
  search_site_count: number;
}

export interface ActiveTask {
  kind: "search" | "recognition" | "library" | "automation";
  id: number;
  status: string;
  total: number;
  completed: number;
  playlist_id: number;
  playlist_name: string;
  trigger?: string;
  stage?: string;
}

export interface Todo {
  key:
    | "search_missing"
    | "pick"
    | "submit"
    | "unrecognized"
    | "unchecked"
    | "no_eligible"
    | "submit_failed"
    | "context_expired"
    | "failing_sites"
    | "empty_sites";
  count: number;
  names?: string[];
  emby_configured?: boolean;
}

export interface HomeData {
  playlists: Playlist[];
  playlist_id: number | null;
  counts: Counts;
  todos: Todo[];
  tasks: ActiveTask[];
  recent: Film[];
  up_next: Film[];
}

export interface ProviderStatus {
  ok?: boolean;
  configured?: boolean;
  message?: string;
}

export interface ConnectionStatus {
  ok: boolean;
  version?: string;
  providers: Record<string, ProviderStatus>;
}

export interface SearchTaskRow {
  id: number;
  status: string;
  total: number;
  completed: number;
}

export type PickBucket = "candidates" | "selected" | "no_eligible";

export interface PickItem extends Film {
  bucket: PickBucket;
  candidates: Candidate[];
  excluded_summary: { reason: string; count: number }[];
  excluded_count: number;
}

export interface PickPage {
  items: PickItem[];
  counts: Record<PickBucket | "all", number>;
  playlists: Playlist[];
}

export interface CartItem {
  id: string;
  title: string;
  site_name: string | null;
  size: number | null;
  rank_no: number | null;
  original_title: string;
  chinese_title: string | null;
  tmdb_title: string | null;
  context_available: boolean;
}

export interface SubmitResult {
  submitted: number;
  needs_research: number;
  skipped: { title: string; reason: string }[];
  blocked_unknown: { title: string; reason: string }[];
}

export interface TimelineEvent {
  id: string;
  at: string | null;
  kind: "submit" | "search" | "recognition" | "library" | "notice" | "system";
  level: "info" | "success" | "warning" | "error";
  title: string;
  detail: string;
  film_id: number | null;
  task_id?: number;
  task_status?: string;
}

export interface TmdbMatch {
  tmdb_id: number;
  title: string;
  original_title: string;
  year: number | null;
  overview: string;
  current: boolean;
}

export interface RuntimeSettings {
  [key: string]: string | number | boolean | null | undefined;
  mp_base_url: string;
  mp_timeout_seconds: number;
  emby_base_url: string;
  tmdb_language: string;
  cookiecloud_url: string;
  cookiecloud_endpoint: string;
  outbound_proxy_url: string;
  tmdb_proxy_enabled: boolean;
  pt_proxy_enabled: boolean;
  ai_base_url: string;
  ai_model: string;
  tr_base_url: string;
  dashboard_random_posters: boolean;
  access_token_required: boolean;
  access_token_strength: "missing" | "weak" | "strong";
  access_token_strength_enforced: boolean;
}

export interface CookieCloudStatus {
  configured: boolean;
  key_valid: boolean;
  url_configured: boolean;
  url: string;
  received: boolean;
  updated_at: string | null;
  endpoint: string;
  last_sync: { at: string; origin: string; updated: string[]; unchanged: number; missing: string[] } | null;
}

export interface Site {
  id: number;
  name: string;
  adapter: string;
  base_url: string;
  api_key: string;
  cookie: string;
  user_agent: string;
  priority: number;
  timeout_seconds: number;
  rss_url: string;
  icon_url: string;
  proxy: number;
  render: number;
  limit_interval: number | null;
  limit_count: number | null;
  enabled: number;
  search_enabled: number;
  migration_note: string | null;
  last_status: string;
  last_message: string | null;
  last_duration_ms: number | null;
  last_tested_at: string | null;
  cookie_updated_at: string | null;
  cookie_source: "cookiecloud" | "manual" | "moviepilot" | null;
  api_key_configured: boolean;
  cookie_configured: boolean;
  rss_url_configured: boolean;
  icon_endpoint: string;
  local_stats: { total: number; succeeded: number; success_rate: number | null; average_ms: number | null; result_count: number; last_attempt_at: string | null };
  account_stats: { uploaded: number | null; downloaded: number | null; ratio: number | null; bonus: number | null; seeding: number | null; checked_at: string | null; error: string | null };
}

export interface PlaylistRow {
  id: number;
  name: string;
  position: number;
  source_type: string | null;
  source_url: string | null;
  source_name: string | null;
  last_synced_at: string | null;
  created_at: string;
  item_count: number;
  recognized_count: number | null;
  sync_enabled?: number;
  sync_interval_hours?: number;
  next_sync_at?: string | null;
  last_sync_status?: string | null;
  last_sync_message?: string | null;
  automation_enabled?: number;
  automation_batch_size?: number;
}

export interface LogEvent {
  ts: string;
  level: string;
  event: string;
  [key: string]: unknown;
}

export interface HistoryRow {
  id: number;
  title: string;
  torrent_name: string;
  site_name: string | null;
  success: number;
  message: string | null;
  created_at: string;
  lifecycle_status: string;
  status_label: string;
  status_source: string;
  status_reason: string;
  next_action: string;
}

export interface SearchTaskDetail {
  id: number;
  status: string;
  total: number;
  completed: number;
  matched: number;
  trigger: string;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  attempt_summary: { total: number; succeeded: number | null; failed: number | null };
}

export interface TaskAttempts {
  items: { id: number; rank_no: number; original_title: string; site_name: string; status: string; result_count: number; duration_ms: number; error_message: string | null }[];
  sites: { site_id: number; site_name: string; total: number; succeeded: number; failed: number; average_ms: number | null }[];
}

export interface TaskLog {
  id: number;
  level: string;
  stage: string;
  message: string;
  created_at: string;
}
