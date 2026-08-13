import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .candidate_policy import DEFAULT_POLICY
from .security import sanitize_sensitive_text
from .util import resource_fingerprint


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
  parent_task_id INTEGER,
  trigger TEXT NOT NULL DEFAULT 'manual',
  site_ids_json TEXT,
  item_ids_json TEXT,
  pair_scope_json TEXT,
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


SCHEMA_VERSION = 3


def initialize() -> None:
    """建表与迁移入口（审计 2-10）：schema_version 用于未来按版本分派迁移。"""
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        previous_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if previous_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库版本 {previous_version} 高于当前程序支持的版本 {SCHEMA_VERSION}，已停止启动以避免降级覆盖"
            )
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # 迁移 1→2：candidates.submitted_at 由下方列补齐逻辑处理（幂等）。
        # 幂等二级索引：随 search_attempts / search_task_logs / candidates 增长，
        # 避免每分钟清理与聚合查询退化为全表扫描。
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_search_attempts_task ON search_attempts(task_id);
            CREATE INDEX IF NOT EXISTS idx_download_history_candidate ON download_history(candidate_id);
            CREATE INDEX IF NOT EXISTS idx_download_history_item ON download_history(playlist_item_id);
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
            -- datetime() 函数索引：cleanup_old_data 的保留窗口比较可走索引（审计 2-15）。
            CREATE INDEX IF NOT EXISTS idx_candidates_created_dt ON candidates(datetime(created_at));
            CREATE INDEX IF NOT EXISTS idx_search_attempts_finished_dt ON search_attempts(datetime(finished_at));
            CREATE INDEX IF NOT EXISTS idx_search_task_logs_created_dt ON search_task_logs(datetime(created_at));
            CREATE INDEX IF NOT EXISTS idx_notifications_created_dt ON notifications(datetime(created_at));
            """
        )
        # A playlist may have at most one active background task of each kind.
        # Older versions only checked this in Python, so two requests could
        # both pass the SELECT-then-INSERT window.  Preserve any duplicate
        # history while retiring older active rows before creating the unique
        # partial indexes used by current writers.
        for table, message_column, message in (
            ("automation_runs", "message", "重复的自动化任务已被中断"),
            ("recognition_tasks", "error_message", "重复的识别任务已被中断"),
            ("library_scan_tasks", "error_message", "重复的入库检查任务已被中断"),
        ):
            duplicate_rows = conn.execute(
                f"""SELECT older.id FROM {table} AS older
                    WHERE older.status IN ('queued','running')
                      AND EXISTS (
                        SELECT 1 FROM {table} AS newer
                        WHERE newer.playlist_id=older.playlist_id
                          AND newer.status IN ('queued','running')
                          AND newer.id > older.id
                      )"""  # nosec B608 - table names are fixed above
            ).fetchall()
            for row in duplicate_rows:
                conn.execute(
                    f"""UPDATE {table}
                        SET status='interrupted',
                            {message_column}=COALESCE(NULLIF({message_column},''),?),
                            updated_at=datetime('now')
                        WHERE id=?""",  # nosec B608 - column names are fixed above
                    (message, row["id"]),
                )
        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_active_playlist
              ON automation_runs(playlist_id) WHERE status IN ('queued','running');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recognition_active_playlist
              ON recognition_tasks(playlist_id) WHERE status IN ('queued','running');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_library_scan_active_playlist
              ON library_scan_tasks(playlist_id) WHERE status IN ('queued','running');
            """
        )
        candidate_columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
        _ = previous_version  # 保留版本号用于后续迁移分派
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
            "detail_url": "TEXT",
            "submitted_at": "TEXT",
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
            "last_status": "TEXT NOT NULL DEFAULT 'untested'",
            "last_message": "TEXT", "last_tested_at": "TEXT",
            "search_enabled": "INTEGER NOT NULL DEFAULT 1", "migration_note": "TEXT",
            "account_uploaded": "INTEGER", "account_downloaded": "INTEGER",
            "account_ratio": "REAL", "account_bonus": "REAL", "account_seeding": "INTEGER",
            "account_stats_checked_at": "TEXT", "account_stats_error": "TEXT",
        }.items():
            if column not in site_columns:
                conn.execute(f"ALTER TABLE pt_sites ADD COLUMN {column} {definition}")
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(search_tasks)")}
        for column, definition in {
            "parent_task_id": "INTEGER", "trigger": "TEXT NOT NULL DEFAULT 'manual'",
            "site_ids_json": "TEXT", "item_ids_json": "TEXT", "pair_scope_json": "TEXT",
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
            "playlist_item_snapshot_json": "TEXT",
            "resource_key": "TEXT",
        }.items():
            if column not in download_columns:
                conn.execute(f"ALTER TABLE download_history ADD COLUMN {column} {definition}")
        if "last_duration_ms" not in site_columns:
            conn.execute("ALTER TABLE pt_sites ADD COLUMN last_duration_ms INTEGER")
        conn.execute(
            """UPDATE download_history
               SET playlist_item_id=(SELECT playlist_item_id FROM candidates WHERE candidates.id=download_history.candidate_id)
               WHERE playlist_item_id IS NULL AND candidate_id IS NOT NULL""",
        )
        # resource_key 必须独立保存在历史表中：候选会按保留策略清理，不能让长期去重
        # 依赖已经不存在的 candidates 行。已有候选的历史可以精确回填；候选已经清理的
        # 旧历史只能用标题生成兼容指纹，后续新写入的历史都会保存精确 key。
        legacy_history = conn.execute(
            """SELECT h.rowid AS _rowid_, h.title, h.torrent_name,
                      c.resource_key AS candidate_resource_key, c.title AS candidate_title, c.size AS candidate_size
                 FROM download_history h
                 LEFT JOIN candidates c ON c.id=h.candidate_id
                WHERE h.resource_key IS NULL OR h.resource_key=''""",
        ).fetchall()
        for row in legacy_history:
            resource_key = row["candidate_resource_key"] or resource_fingerprint(
                row["candidate_title"] or row["torrent_name"] or row["title"] or "", row["candidate_size"]
            )
            if resource_key:
                conn.execute(
                    "UPDATE download_history SET resource_key=? WHERE rowid=?",
                    (resource_key, row["_rowid_"]),
                )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_history_resource_key ON download_history(resource_key)")
        # 历史版本曾允许自动把推荐候选加入下载列表；升级后统一恢复为人工确认。
        conn.execute("UPDATE playlists SET automation_auto_cart=0 WHERE automation_auto_cart!=0")
        defaults = {
            # 旧版评分键已废弃（审计 2-7）：仅保留候选策略作为唯一评分配置源。
            "candidate_limit": "6",
            "candidate_policy": json.dumps(DEFAULT_POLICY, ensure_ascii=False, separators=(",", ":")),
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO app_config(key, value) VALUES (?, ?)", (key, value))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        # 0.35 起不再经 MoviePilot 搜索。旧映射只保留为待补认证模板，绝不静默搜索。
        conn.execute(
            "UPDATE pt_sites SET adapter=CASE WHEN lower(base_url) LIKE '%m-team%' THEN 'mteam' ELSE 'nexusphp' END, "
            "enabled=1, search_enabled=0, migration_note='旧站点映射已迁移，请补充独立认证后再参与搜索' "
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
