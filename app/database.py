import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .candidate_policy import DEFAULT_POLICY
from . import migrations
from .migrations import SCHEMA_VERSION


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  source_type TEXT,
  source_url TEXT,
  source_name TEXT,
  last_synced_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  rank_no INTEGER,
  imdb_id TEXT,
  original_title TEXT NOT NULL,
  year INTEGER,
  chinese_title TEXT,
  tmdb_id INTEGER,
  tmdb_title TEXT,
  tmdb_original_title TEXT,
  tmdb_year INTEGER,
  tmdb_imdb_id TEXT,
  tmdb_checked_at TEXT,
  tmdb_poster_path TEXT,
  fanart_poster_url TEXT,
  tmdb_original_language TEXT,
  source_tmdb_id INTEGER,
  source_ref TEXT,
  fanart_backdrop_url TEXT,
  tmdb_backdrop_path TEXT,
  tmdb_alt_titles_json TEXT,
  emby_item_id TEXT,
  emby_image_tag TEXT,
  library_state TEXT NOT NULL DEFAULT 'unknown',
  library_checked_at TEXT,
  UNIQUE(playlist_id, rank_no)
);
CREATE TABLE IF NOT EXISTS library_scan_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  total INTEGER NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0,
  in_library INTEGER NOT NULL DEFAULT 0,
  strm INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  range_start INTEGER NOT NULL,
  range_end INTEGER NOT NULL,
  status TEXT NOT NULL,
  total INTEGER NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0,
  matched INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  parent_task_id INTEGER,
  trigger TEXT NOT NULL DEFAULT 'manual',
  site_ids_json TEXT,
  item_ids_json TEXT,
  pair_scope_json TEXT,
  done_item_ids_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_task_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES search_tasks(id) ON DELETE CASCADE,
  level TEXT NOT NULL,
  stage TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES search_tasks(id) ON DELETE CASCADE,
  playlist_item_id INTEGER NOT NULL REFERENCES playlist_items(id) ON DELETE CASCADE,
  site_id INTEGER REFERENCES pt_sites(id) ON DELETE SET NULL,
  site_name TEXT NOT NULL,
  attempt_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  result_count INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  query_count INTEGER NOT NULL DEFAULT 1,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recognition_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  total INTEGER NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0,
  matched INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'missing',
  corrected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES search_tasks(id) ON DELETE CASCADE,
  playlist_item_id INTEGER NOT NULL REFERENCES playlist_items(id) ON DELETE CASCADE,
  candidate_index INTEGER NOT NULL,
  title TEXT NOT NULL,
  site_name TEXT,
  size INTEGER,
  seeders INTEGER,
  resolution TEXT,
  codec TEXT,
  group_name TEXT,
  group_tier INTEGER NOT NULL DEFAULT 9,
  score INTEGER NOT NULL DEFAULT 0,
  score_breakdown TEXT,
  ranking INTEGER NOT NULL,
  recommendation TEXT NOT NULL DEFAULT 'manual',
  recommendation_reason TEXT,
  resource_key TEXT,
  library_state TEXT NOT NULL DEFAULT 'unknown',
  is_manual_only INTEGER NOT NULL DEFAULT 0,
  eligibility TEXT NOT NULL DEFAULT 'eligible',
  exclusion_reason TEXT,
  profile_id TEXT,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_contexts (
  candidate_id TEXT PRIMARY KEY,
  payload BLOB NOT NULL,
  key_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS selection_items (
  candidate_id TEXT PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
  selected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id TEXT,
  playlist_item_id INTEGER,
  playlist_item_snapshot_json TEXT,
  resource_key TEXT,
  title TEXT NOT NULL,
  torrent_name TEXT NOT NULL,
  site_name TEXT,
  submission_hash TEXT,
  success INTEGER NOT NULL,
  message TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  trigger TEXT NOT NULL DEFAULT 'manual',
  status TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'queued',
  total INTEGER NOT NULL DEFAULT 0,
  completed INTEGER NOT NULL DEFAULT 0,
  recognized INTEGER NOT NULL DEFAULT 0,
  searched INTEGER NOT NULL DEFAULT 0,
  recommended INTEGER NOT NULL DEFAULT 0,
  message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  level TEXT NOT NULL DEFAULT 'info',
  title TEXT NOT NULL,
  message TEXT NOT NULL,
  read INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pt_sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  adapter TEXT NOT NULL,
  base_url TEXT NOT NULL DEFAULT '',
  api_key TEXT NOT NULL DEFAULT '',
  cookie TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 100,
  timeout_seconds INTEGER NOT NULL DEFAULT 30,
  rss_url TEXT NOT NULL DEFAULT '',
  icon_url TEXT NOT NULL DEFAULT '',
  proxy INTEGER NOT NULL DEFAULT 0,
  render INTEGER NOT NULL DEFAULT 0,
  limit_interval INTEGER,
  limit_count INTEGER,
  search_enabled INTEGER NOT NULL DEFAULT 1,
  migration_note TEXT,
  last_status TEXT NOT NULL DEFAULT 'untested',
  last_message TEXT,
  last_duration_ms INTEGER,
  last_tested_at TEXT,
  account_uploaded INTEGER,
  account_downloaded INTEGER,
  account_ratio REAL,
  account_bonus REAL,
  account_seeding INTEGER,
  account_stats_checked_at TEXT,
  account_stats_error TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
"""


# 普通索引与“同一片单同类任务只能有一个在进行”的唯一部分索引；每次启动幂等创建。
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_search_attempts_task ON search_attempts(task_id);
CREATE INDEX IF NOT EXISTS idx_download_history_candidate ON download_history(candidate_id);
CREATE INDEX IF NOT EXISTS idx_download_history_item ON download_history(playlist_item_id);
CREATE INDEX IF NOT EXISTS idx_download_history_resource_key ON download_history(resource_key);
CREATE INDEX IF NOT EXISTS idx_search_attempts_site ON search_attempts(site_id);
CREATE INDEX IF NOT EXISTS idx_search_task_logs_task ON search_task_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_candidates_task ON candidates(task_id);
CREATE INDEX IF NOT EXISTS idx_candidates_item ON candidates(playlist_item_id);
CREATE INDEX IF NOT EXISTS idx_search_tasks_status_updated ON search_tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_search_tasks_updated_dt ON search_tasks(datetime(updated_at));
CREATE INDEX IF NOT EXISTS idx_search_tasks_playlist ON search_tasks(playlist_id);
CREATE INDEX IF NOT EXISTS idx_library_scan_tasks_playlist ON library_scan_tasks(playlist_id);
CREATE INDEX IF NOT EXISTS idx_recognition_tasks_playlist ON recognition_tasks(playlist_id);
CREATE INDEX IF NOT EXISTS idx_automation_runs_playlist ON automation_runs(playlist_id);
CREATE INDEX IF NOT EXISTS idx_notifications_read_created ON notifications(read, created_at);
CREATE INDEX IF NOT EXISTS idx_playlist_items_playlist_rank ON playlist_items(playlist_id, rank_no);
-- datetime() 函数索引：cleanup_old_data 的保留窗口比较可走索引。
CREATE INDEX IF NOT EXISTS idx_candidates_created_dt ON candidates(datetime(created_at));
CREATE INDEX IF NOT EXISTS idx_search_attempts_finished_dt ON search_attempts(datetime(finished_at));
CREATE INDEX IF NOT EXISTS idx_search_task_logs_created_dt ON search_task_logs(datetime(created_at));
CREATE INDEX IF NOT EXISTS idx_notifications_created_dt ON notifications(datetime(created_at));
CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_active_playlist
  ON automation_runs(playlist_id) WHERE status IN ('queued','running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_recognition_active_playlist
  ON recognition_tasks(playlist_id) WHERE status IN ('queued','running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_library_scan_active_playlist
  ON library_scan_tasks(playlist_id) WHERE status IN ('queued','running');
"""


def initialize() -> None:
    """建表与迁移入口：建表前的迁移 → 建表 → 补齐新增列 → 按编号迁移 → 索引 → 默认配置。"""
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        previous_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if previous_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库版本 {previous_version} 高于当前程序支持的版本 {SCHEMA_VERSION}，已停止启动以避免降级覆盖"
            )
        conn.execute("PRAGMA journal_mode = WAL")
        for migration in migrations.pending(previous_version, before_schema=True):
            migration.apply(conn)
        conn.executescript(SCHEMA)
        migrations.add_missing_columns(conn)
        for migration in migrations.pending(previous_version, before_schema=False):
            migration.apply(conn)
        conn.executescript(INDEXES)
        defaults = {
            "candidate_limit": "6",
            "candidate_policy": json.dumps(DEFAULT_POLICY, ensure_ascii=False, separators=(",", ":")),
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO app_config(key, value) VALUES (?, ?)", (key, value))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    (Path(settings.data_dir) / "playlist-autodown.db").chmod(0o600)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(Path(settings.data_dir) / "playlist-autodown.db", timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def config_values() -> dict[str, str]:
    with connect() as conn:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM app_config")}


def save_config(values: dict[str, str]) -> None:
    allowed = {
        "candidate_limit", "candidate_policy",
    }
    with connect() as conn:
        for key, value in values.items():
            if key in allowed:
                conn.execute("INSERT INTO app_config(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))



def cleanup_old_data() -> int:
    """Remove old short-lived search diagnostics and notifications.
    Retention is generous to preserve enough data for inspection; adjust as needed.
    """
    with connect() as conn:
        total = 0
        # 失败/中断任务保留候选与诊断数据，只从常用任务列表中归档，避免长期显示为挂起。
        total += conn.execute(
            """UPDATE search_tasks SET status='archived'
               WHERE status IN ('failed','partial','interrupted','cancelled')
                 AND datetime(updated_at) < datetime('now', '-24 hours')"""
        ).rowcount
        # completed 任务 30 天后归档，避免 search_tasks 无界增长（审计 2-14）。
        total += conn.execute(
            """UPDATE search_tasks SET status='archived'
               WHERE status='completed'
                 AND datetime(updated_at) < datetime('now', '-30 days')"""
        ).rowcount
        # 候选按创建时间统一清理（含 completed 任务，审计 2-14），避免 candidates 无界增长。
        total += conn.execute(
            "DELETE FROM candidates WHERE datetime(created_at) < datetime('now', '-30 days')"
        ).rowcount
        total += conn.execute(
            "DELETE FROM search_attempts WHERE datetime(finished_at) < datetime('now', '-30 days')"
        ).rowcount
        total += conn.execute(
            "DELETE FROM search_task_logs WHERE datetime(created_at) < datetime('now', '-14 days')"
        ).rowcount
        total += conn.execute(
            "DELETE FROM notifications WHERE datetime(created_at) < datetime('now', '-30 days')"
        ).rowcount
        return total


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
