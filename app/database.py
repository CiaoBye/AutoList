import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .candidate_policy import DEFAULT_POLICY
from .security import sanitize_sensitive_text


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
  updated_at TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS cart_items (
  candidate_id TEXT PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
  selected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id TEXT,
  playlist_item_id INTEGER,
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
  mp_site_id INTEGER,
  search_enabled INTEGER NOT NULL DEFAULT 1,
  migration_note TEXT,
  last_status TEXT NOT NULL DEFAULT 'untested',
  last_message TEXT,
  last_tested_at TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
"""


def initialize() -> None:
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        candidate_columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
        for column, definition in {
            "codec": "TEXT",
            "score": "INTEGER NOT NULL DEFAULT 0",
            "score_breakdown": "TEXT",
            "recommendation": "TEXT NOT NULL DEFAULT 'manual'",
            "recommendation_reason": "TEXT",
            "resource_key": "TEXT",
            "eligibility": "TEXT NOT NULL DEFAULT 'eligible'",
            "exclusion_reason": "TEXT",
            "profile_id": "TEXT",
        }.items():
            if column not in candidate_columns:
                conn.execute(f"ALTER TABLE candidates ADD COLUMN {column} {definition}")
        playlist_columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlists)")}
        for column, definition in {
            "position": "INTEGER NOT NULL DEFAULT 0", "source_type": "TEXT", "source_url": "TEXT",
            "source_name": "TEXT", "last_synced_at": "TEXT",
            "automation_enabled": "INTEGER NOT NULL DEFAULT 0", "automation_auto_cart": "INTEGER NOT NULL DEFAULT 0",
            "automation_batch_size": "INTEGER NOT NULL DEFAULT 50", "sync_enabled": "INTEGER NOT NULL DEFAULT 0",
            "sync_interval_hours": "INTEGER NOT NULL DEFAULT 24", "next_sync_at": "TEXT",
            "last_sync_status": "TEXT", "last_sync_message": "TEXT",
        }.items():
            if column not in playlist_columns:
                conn.execute(f"ALTER TABLE playlists ADD COLUMN {column} {definition}")
        playlist_item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlist_items)")}
        for column, definition in {
            "library_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "library_checked_at": "TEXT",
            "emby_item_id": "TEXT",
            "emby_image_tag": "TEXT",
            "tmdb_title": "TEXT",
            "tmdb_original_title": "TEXT",
            "tmdb_year": "INTEGER",
            "tmdb_imdb_id": "TEXT",
            "tmdb_checked_at": "TEXT",
        }.items():
            if column not in playlist_item_columns:
                conn.execute(f"ALTER TABLE playlist_items ADD COLUMN {column} {definition}")
        site_columns = {row["name"] for row in conn.execute("PRAGMA table_info(pt_sites)")}
        for column, definition in {
            "user_agent": "TEXT NOT NULL DEFAULT ''", "priority": "INTEGER NOT NULL DEFAULT 100",
            "timeout_seconds": "INTEGER NOT NULL DEFAULT 30", "rss_url": "TEXT NOT NULL DEFAULT ''",
            "icon_url": "TEXT NOT NULL DEFAULT ''", "proxy": "INTEGER NOT NULL DEFAULT 0",
            "render": "INTEGER NOT NULL DEFAULT 0", "limit_interval": "INTEGER", "limit_count": "INTEGER",
            "mp_site_id": "INTEGER", "last_status": "TEXT NOT NULL DEFAULT 'untested'",
            "last_message": "TEXT", "last_tested_at": "TEXT",
            "search_enabled": "INTEGER NOT NULL DEFAULT 1", "migration_note": "TEXT",
        }.items():
            if column not in site_columns:
                conn.execute(f"ALTER TABLE pt_sites ADD COLUMN {column} {definition}")
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(search_tasks)")}
        for column, definition in {
            "parent_task_id": "INTEGER", "trigger": "TEXT NOT NULL DEFAULT 'manual'",
            "site_ids_json": "TEXT", "item_ids_json": "TEXT",
        }.items():
            if column not in task_columns:
                conn.execute(f"ALTER TABLE search_tasks ADD COLUMN {column} {definition}")
        attempt_columns = {row["name"] for row in conn.execute("PRAGMA table_info(search_attempts)")}
        if "query_count" not in attempt_columns:
            conn.execute("ALTER TABLE search_attempts ADD COLUMN query_count INTEGER NOT NULL DEFAULT 1")
        download_columns = {row["name"] for row in conn.execute("PRAGMA table_info(download_history)")}
        for column, definition in {
            "playlist_item_id": "INTEGER",
            "submission_hash": "TEXT",
        }.items():
            if column not in download_columns:
                conn.execute(f"ALTER TABLE download_history ADD COLUMN {column} {definition}")
        conn.execute(
            """UPDATE download_history
               SET playlist_item_id=(SELECT playlist_item_id FROM candidates WHERE candidates.id=download_history.candidate_id)
               WHERE playlist_item_id IS NULL AND candidate_id IS NOT NULL""",
        )
        defaults = {
            "preferred_resolutions": "2160p,1080p",
            "minimum_resolution": "1080p",
            "preferred_codecs": "x265,x264",
            "priority_groups": "FRDS,ADE",
            "secondary_groups": "HDS,CHD",
            "fallback_groups": "CMCT",
            "allow_unknown_groups": "false",
            "candidate_limit": "6",
            "scoring_policy": json.dumps({
                "resolutions": {"2160p": {"enabled": True, "score": 40}, "1080p": {"enabled": True, "score": 28}, "720p": {"enabled": False, "score": 0}},
                "codecs": {"x265": {"enabled": True, "score": 18}, "x264": {"enabled": True, "score": 12}},
                "groups": [
                    {"name": "FRDS", "enabled": True, "score": 25, "tier": 1}, {"name": "ADE", "enabled": True, "score": 25, "tier": 1},
                    {"name": "HDS", "enabled": True, "score": 16, "tier": 2}, {"name": "CHD", "enabled": True, "score": 16, "tier": 2},
                    {"name": "CMCT", "enabled": True, "score": 8, "tier": 3},
                ],
                "sources": {"REMUX": {"enabled": True, "score": 15}, "BLURAY": {"enabled": True, "score": 12}, "WEB-DL": {"enabled": True, "score": 8}, "WEBRIP": {"enabled": True, "score": 4}},
                "minimum_resolution": "1080p", "allow_unknown_groups": False, "auto_threshold": 70,
            }, ensure_ascii=False, separators=(",", ":")),
            "candidate_policy": json.dumps(DEFAULT_POLICY, ensure_ascii=False, separators=(",", ":")),
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO app_config(key, value) VALUES (?, ?)", (key, value))
        # 0.35 起不再经 MoviePilot 搜索。旧映射只保留为待补认证模板，绝不静默搜索。
        conn.execute(
            "UPDATE pt_sites SET adapter=CASE WHEN lower(base_url) LIKE '%m-team%' THEN 'mteam' ELSE 'nexusphp' END, "
            "enabled=1, search_enabled=0, migration_note='由 MoviePilot 映射迁移，请补充独立认证后再参与搜索', mp_site_id=NULL "
            "WHERE adapter IN ('moviepilot','moviepilot_site')"
        )
        conn.execute(
            """INSERT INTO search_task_logs(task_id,level,stage,message,created_at)
               SELECT id,'error','legacy',COALESCE(NULLIF(error_message,''),'旧任务异常停止，历史版本未记录详细阶段'),updated_at
               FROM search_tasks t WHERE status='failed' AND NOT EXISTS(
                 SELECT 1 FROM search_task_logs l WHERE l.task_id=t.id
               )"""
        )
        # 旧版本仅在建表连接启用外键，普通连接的级联删除没有生效；启动时清理历史孤立行。
        conn.execute("DELETE FROM search_task_logs WHERE task_id NOT IN (SELECT id FROM search_tasks)")
        conn.execute("DELETE FROM search_attempts WHERE task_id NOT IN (SELECT id FROM search_tasks)")
        conn.execute("DELETE FROM recognition_tasks WHERE playlist_id NOT IN (SELECT id FROM playlists)")
        conn.execute("DELETE FROM library_scan_tasks WHERE playlist_id NOT IN (SELECT id FROM playlists)")
        conn.execute("DELETE FROM playlist_items WHERE playlist_id NOT IN (SELECT id FROM playlists)")
        # 防御性清理历史版本可能遗留的错误文本，避免接口继续暴露旧凭据。
        for select_sql, update_sql, column in (
            ("SELECT rowid AS _rowid_,message FROM download_history WHERE message IS NOT NULL AND message!=''", "UPDATE download_history SET message=? WHERE rowid=?", "message"),
            ("SELECT rowid AS _rowid_,error_message FROM search_tasks WHERE error_message IS NOT NULL AND error_message!=''", "UPDATE search_tasks SET error_message=? WHERE rowid=?", "error_message"),
            ("SELECT rowid AS _rowid_,message FROM search_task_logs WHERE message IS NOT NULL AND message!=''", "UPDATE search_task_logs SET message=? WHERE rowid=?", "message"),
            ("SELECT rowid AS _rowid_,error_message FROM recognition_tasks WHERE error_message IS NOT NULL AND error_message!=''", "UPDATE recognition_tasks SET error_message=? WHERE rowid=?", "error_message"),
            ("SELECT rowid AS _rowid_,error_message FROM library_scan_tasks WHERE error_message IS NOT NULL AND error_message!=''", "UPDATE library_scan_tasks SET error_message=? WHERE rowid=?", "error_message"),
            ("SELECT rowid AS _rowid_,last_message FROM pt_sites WHERE last_message IS NOT NULL AND last_message!=''", "UPDATE pt_sites SET last_message=? WHERE rowid=?", "last_message"),
            ("SELECT rowid AS _rowid_,error_message FROM search_attempts WHERE error_message IS NOT NULL AND error_message!=''", "UPDATE search_attempts SET error_message=? WHERE rowid=?", "error_message"),
            ("SELECT rowid AS _rowid_,message FROM notifications WHERE message IS NOT NULL AND message!=''", "UPDATE notifications SET message=? WHERE rowid=?", "message"),
        ):
            rows = conn.execute(select_sql).fetchall()
            for row in rows:
                sanitized = sanitize_sensitive_text(row[column], 1000 if column == "message" else 500)
                if sanitized != row[column]:
                    conn.execute(update_sql, (sanitized, row["_rowid_"]))
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
        "preferred_resolutions", "minimum_resolution", "preferred_codecs", "priority_groups",
        "secondary_groups", "fallback_groups", "allow_unknown_groups", "candidate_limit", "scoring_policy",
        "candidate_policy",
    }
    with connect() as conn:
        for key, value in values.items():
            if key in allowed:
                conn.execute("INSERT INTO app_config(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))



def cleanup_old_data() -> int:
    """Remove old search attempts, candidates, download history, task logs, and notifications.
    Retention is generous to preserve enough data for inspection; adjust as needed.
    Use SQLite datetime arithmetic instead of Python clock to remain consistent with
    the UTC strings stored in the database.
    """
    with connect() as conn:
        total = 0
        total += conn.execute(
            "DELETE FROM search_attempts WHERE finished_at < datetime('now', '-30 days')"
        ).rowcount
        total += conn.execute(
            "DELETE FROM candidates WHERE created_at < datetime('now', '-30 days') AND id NOT IN (SELECT candidate_id FROM cart_items)"
        ).rowcount
        total += conn.execute(
            "DELETE FROM search_task_logs WHERE created_at < datetime('now', '-14 days')"
        ).rowcount
        total += conn.execute(
            "DELETE FROM download_history WHERE created_at < datetime('now', '-90 days')"
        ).rowcount
        total += conn.execute(
            "DELETE FROM notifications WHERE created_at < datetime('now', '-30 days')"
        ).rowcount
        return total


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
