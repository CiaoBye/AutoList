"""数据库迁移：按编号排列的步骤。

每一步是一个函数，只在数据库版本（``PRAGMA user_version``）低于它的编号时执行一次；程序支持的
数据库版本就是最大的编号。可以安全重复执行的部分（补齐新增列、建索引）每次启动都做，不占编号。

新增迁移：在 ``MIGRATIONS`` 末尾追加一个更大编号的步骤，并在 ``tests/test_migrations.py`` 补测试。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from .security import sanitize_sensitive_text
from .util import resource_fingerprint

# 旧版本建的表缺少的列：按表列出，启动时补齐（ALTER TABLE ADD COLUMN），与版本号无关。
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "candidates": {
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
    },
    "playlists": {
        "position": "INTEGER NOT NULL DEFAULT 0", "source_type": "TEXT", "source_url": "TEXT",
        "source_name": "TEXT", "last_synced_at": "TEXT",
        "automation_enabled": "INTEGER NOT NULL DEFAULT 0", "automation_auto_select": "INTEGER NOT NULL DEFAULT 0",
        "automation_batch_size": "INTEGER NOT NULL DEFAULT 50", "sync_enabled": "INTEGER NOT NULL DEFAULT 0",
        "sync_interval_hours": "INTEGER NOT NULL DEFAULT 24", "next_sync_at": "TEXT",
        "last_sync_status": "TEXT", "last_sync_message": "TEXT",
    },
    "playlist_items": {
        "library_state": "TEXT NOT NULL DEFAULT 'unknown'",
        "library_checked_at": "TEXT",
        "emby_item_id": "TEXT",
        "emby_image_tag": "TEXT",
        "tmdb_title": "TEXT",
        "tmdb_original_title": "TEXT",
        "tmdb_year": "INTEGER",
        "tmdb_imdb_id": "TEXT",
        "tmdb_checked_at": "TEXT",
        # TMDB 海报路径（如 /abc.jpg）。NULL = 未取过，'' = TMDB 没有海报。
        "tmdb_poster_path": "TEXT",
        # fanart.tv 海报地址。NULL = 未查过，'' = fanart.tv 没有海报。
        "fanart_poster_url": "TEXT",
        # TMDB 原语言（ISO 639-1），fanart 海报按原语言挑选。NULL = 尚未取得。
        "tmdb_original_language": "TEXT",
        # 来源提供的身份：source_tmdb_id 为来源自带的 TMDB 编号（与识别结果 tmdb_id 分开保存），
        # source_ref 为来源内的影片标识（如 letterboxd:cure），识别时据此向来源补取编号。
        "source_tmdb_id": "INTEGER",
        "source_ref": "TEXT",
        # 影片详情横幅剧照。NULL = 未查过，'' = 没有。
        "fanart_backdrop_url": "TEXT",
        "tmdb_backdrop_path": "TEXT",
        # TMDB 其他片名（JSON 数组），寻片比对种子标题时也认这些名字。NULL = 未取过。
        "tmdb_alt_titles_json": "TEXT",
    },
    "pt_sites": {
        "user_agent": "TEXT NOT NULL DEFAULT ''", "priority": "INTEGER NOT NULL DEFAULT 100",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 30", "rss_url": "TEXT NOT NULL DEFAULT ''",
        "icon_url": "TEXT NOT NULL DEFAULT ''", "proxy": "INTEGER NOT NULL DEFAULT 0",
        "render": "INTEGER NOT NULL DEFAULT 0", "limit_interval": "INTEGER", "limit_count": "INTEGER",
        "last_status": "TEXT NOT NULL DEFAULT 'untested'",
        "last_message": "TEXT", "last_tested_at": "TEXT", "last_duration_ms": "INTEGER",
        "search_enabled": "INTEGER NOT NULL DEFAULT 1", "migration_note": "TEXT",
        # 仅补缺：其他站点搜完后，只为可选种子不足的影片按 IMDb 补搜（适合有搜索人机验证、搜索次数有限的站点）。
        "supplement_only": "INTEGER NOT NULL DEFAULT 0",
        "account_uploaded": "INTEGER", "account_downloaded": "INTEGER",
        "account_ratio": "REAL", "account_bonus": "REAL", "account_seeding": "INTEGER",
        "account_stats_checked_at": "TEXT", "account_stats_error": "TEXT",
        # Cookie 最近一次变化的时间与来源（cookiecloud / manual / moviepilot）。
        "cookie_updated_at": "TEXT", "cookie_source": "TEXT",
    },
    "search_tasks": {
        "parent_task_id": "INTEGER", "trigger": "TEXT NOT NULL DEFAULT 'manual'",
        "site_ids_json": "TEXT", "item_ids_json": "TEXT", "pair_scope_json": "TEXT",
        # 已搜完的影片：任务进行中这些影片不再算“寻片中”，候选立即出现在挑选台。
        "done_item_ids_json": "TEXT",
    },
    "search_attempts": {"query_count": "INTEGER NOT NULL DEFAULT 1"},
    "download_history": {
        "playlist_item_id": "INTEGER",
        "submission_hash": "TEXT",
        "playlist_item_snapshot_json": "TEXT",
        "resource_key": "TEXT",
    },
    # 种子识别的结果是电影（movie）还是剧集（tv）；NULL = 未识别出类型。电影与剧集的 TMDB 编号各成一套，必须带上类型。
    "torrent_media": {"media_type": "TEXT"},
    "recognition_tasks": {
        # missing = 只识别未识别的影片；verify = 按 IMDb / 来源编号校准整份片单。
        "mode": "TEXT NOT NULL DEFAULT 'missing'",
        "corrected": "INTEGER NOT NULL DEFAULT 0",
    },
}


def add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]
    # 需要在建表脚本之前执行的步骤（例如改表名，否则会先建出空的新表）。
    before_schema: bool = False


def _recompute_site_adapters(conn: sqlite3.Connection) -> None:
    # 有 Cookie 的站点走 NexusPHP；只有 RSS 地址、没有 Cookie 的站点保持 RSS。
    # 同时修复曾在每次启动时把纯 RSS 站点误改为 nexusphp 的旧迁移结果。
    conn.execute(
        """UPDATE pt_sites
           SET adapter = CASE
               WHEN lower(base_url) LIKE '%m-team%' OR lower(base_url) LIKE '%mteam%' THEN 'mteam'
               WHEN lower(base_url) LIKE '%torznab%' OR lower(base_url) LIKE '%api?t=%'
                    OR lower(base_url) LIKE '%t=caps%' THEN 'torznab'
               WHEN trim(COALESCE(cookie, '')) = '' AND trim(COALESCE(rss_url, '')) != '' THEN 'rss'
               ELSE 'nexusphp'
           END
           WHERE adapter IN ('rss', 'nexusphp')""",
    )
    # 旧版本直接保存了 httpx 原始英文错误；清空后由调度器按新的中文诊断重新读取。
    conn.execute(
        "UPDATE pt_sites SET account_stats_error=NULL, account_stats_checked_at=NULL WHERE account_stats_error IS NOT NULL"
    )


def _reselect_fanart_posters(conn: sqlite3.Connection) -> None:
    # 海报改为按原语言挑选，已选的 fanart 海报全部重新挑选；
    # 同时清掉 1.48 因地址校验漏掉新格式而误记的“没有海报”。
    conn.execute("UPDATE playlist_items SET fanart_poster_url=NULL")


def _rename_selection_storage(conn: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "cart_items" in tables and "selection_items" not in tables:
        conn.execute("ALTER TABLE cart_items RENAME TO selection_items")
    if "playlists" in tables:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(playlists)").fetchall()}
        if "automation_auto_cart" in columns and "automation_auto_select" not in columns:
            conn.execute("ALTER TABLE playlists RENAME COLUMN automation_auto_cart TO automation_auto_select")


def _retire_duplicate_active_tasks(conn: sqlite3.Connection) -> None:
    # 旧版本只在 Python 里检查“同一片单同类任务只能有一个在进行”，两个请求可能同时通过；
    # 保留历史，把较早的重复任务标为中断，之后才能建立唯一部分索引。
    for table, message_column, message in (
        ("automation_runs", "message", "重复的自动化任务已被中断"),
        ("recognition_tasks", "error_message", "重复的识别任务已被中断"),
        ("library_scan_tasks", "error_message", "重复的入库检查任务已被中断"),
    ):
        conn.execute(
            f"""UPDATE {table}
                SET status='interrupted', {message_column}=COALESCE(NULLIF({message_column},''),?), updated_at=datetime('now')
                WHERE status IN ('queued','running') AND EXISTS (
                  SELECT 1 FROM {table} AS newer
                  WHERE newer.playlist_id={table}.playlist_id AND newer.status IN ('queued','running') AND newer.id > {table}.id
                )""",  # nosec B608 - table and column names are fixed above
            (message,),
        )


def _backfill_download_history(conn: sqlite3.Connection) -> None:
    conn.execute(
        """UPDATE download_history
           SET playlist_item_id=(SELECT playlist_item_id FROM candidates WHERE candidates.id=download_history.candidate_id)
           WHERE playlist_item_id IS NULL AND candidate_id IS NOT NULL""",
    )
    # resource_key 必须独立保存在历史表中：候选会按保留策略清理，长期去重不能依赖已经不存在的候选行。
    # 候选还在的历史精确回填；候选已清理的旧历史只能用标题生成兼容指纹。
    for row in conn.execute(
        """SELECT h.rowid AS _rowid_, h.title, h.torrent_name,
                  c.resource_key AS candidate_resource_key, c.title AS candidate_title, c.size AS candidate_size
             FROM download_history h LEFT JOIN candidates c ON c.id=h.candidate_id
            WHERE h.resource_key IS NULL OR h.resource_key=''""",
    ).fetchall():
        resource_key = row["candidate_resource_key"] or resource_fingerprint(
            row["candidate_title"] or row["torrent_name"] or row["title"] or "", row["candidate_size"]
        )
        if resource_key:
            conn.execute("UPDATE download_history SET resource_key=? WHERE rowid=?", (resource_key, row["_rowid_"]))


def _retire_legacy_automation_and_adapters(conn: sqlite3.Connection) -> None:
    # 历史版本曾允许自动把推荐候选加入待入馆清单；统一恢复为人工确认。
    conn.execute("UPDATE playlists SET automation_auto_select=0 WHERE automation_auto_select!=0")
    # 0.35 起不再经 MoviePilot 搜索。旧映射只保留为待补认证模板，绝不静默搜索。
    conn.execute(
        "UPDATE pt_sites SET adapter=CASE WHEN lower(base_url) LIKE '%m-team%' THEN 'mteam' ELSE 'nexusphp' END, "
        "enabled=1, search_enabled=0, migration_note='旧站点映射已迁移，请补充独立认证后再参与搜索' "
        "WHERE adapter IN ('moviepilot','moviepilot_site')"
    )


def _log_legacy_failed_tasks(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO search_task_logs(task_id,level,stage,message,created_at)
           SELECT id,'error','legacy',COALESCE(NULLIF(error_message,''),'旧任务异常停止，历史版本未记录详细阶段'),updated_at
           FROM search_tasks t WHERE status='failed' AND NOT EXISTS(SELECT 1 FROM search_task_logs l WHERE l.task_id=t.id)"""
    )


def _delete_orphan_rows(conn: sqlite3.Connection) -> None:
    # 旧版本仅在建表连接启用外键，普通连接的级联删除没有生效。
    conn.execute("DELETE FROM search_task_logs WHERE task_id NOT IN (SELECT id FROM search_tasks)")
    conn.execute("DELETE FROM search_attempts WHERE task_id NOT IN (SELECT id FROM search_tasks)")
    conn.execute("DELETE FROM recognition_tasks WHERE playlist_id NOT IN (SELECT id FROM playlists)")
    conn.execute("DELETE FROM library_scan_tasks WHERE playlist_id NOT IN (SELECT id FROM playlists)")
    conn.execute("DELETE FROM playlist_items WHERE playlist_id NOT IN (SELECT id FROM playlists)")


def _sanitize_stored_messages(conn: sqlite3.Connection) -> None:
    # 历史版本可能把带凭据的错误原文写进了库，清理后接口不会再暴露。
    for table, column, limit in (
        ("download_history", "message", 1000),
        ("search_tasks", "error_message", 500),
        ("search_task_logs", "message", 1000),
        ("recognition_tasks", "error_message", 500),
        ("library_scan_tasks", "error_message", 500),
        ("pt_sites", "last_message", 500),
        ("search_attempts", "error_message", 500),
        ("notifications", "message", 1000),
    ):
        for row in conn.execute(
            f"SELECT rowid AS _rowid_,{column} AS value FROM {table} WHERE {column} IS NOT NULL AND {column}!=''"  # nosec B608
        ).fetchall():
            sanitized = sanitize_sensitive_text(row["value"], limit)
            if sanitized != row["value"]:
                conn.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (sanitized, row["_rowid_"]))  # nosec B608


def _forget_torrent_media_without_tmdb(conn: sqlite3.Connection) -> None:
    # 1.86–1.87 只读 MoviePilot 下载历史的 tmdbid，新版 MoviePilot 改为 media_source + media_id，
    # 缓存里的识别结果都没有 TMDB 编号、对不上片单影片；删掉后下次打开下载页重新识别。
    conn.execute("DELETE FROM torrent_media WHERE source='moviepilot' AND tmdb_id IS NULL")


def _forget_untyped_torrent_media(conn: sqlite3.Connection) -> None:
    # 种子识别缓存没有记录“电影还是剧集”，剧集的 TMDB 编号被当成同号的电影（黑道家族 → 潜行者）；
    # 缓存只是识别结果，全部删除后带类型重新识别。
    conn.execute("DELETE FROM torrent_media")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(4, "按 Cookie 与 RSS 重算站点适配器，清空旧的英文账户错误", _recompute_site_adapters),
    Migration(7, "海报改为按原语言挑选，重新挑选 fanart 海报", _reselect_fanart_posters),
    Migration(13, "“下载车”改名为待入馆清单（selection）", _rename_selection_storage, before_schema=True),
    Migration(14, "中断同一片单重复的进行中任务", _retire_duplicate_active_tasks),
    Migration(15, "提交记录回填影片编号与资源指纹", _backfill_download_history),
    Migration(16, "停用自动加入清单与经 MoviePilot 搜索的旧设置", _retire_legacy_automation_and_adapters),
    Migration(17, "为旧的失败寻片任务补一条日志", _log_legacy_failed_tasks),
    Migration(18, "清理外键未生效时遗留的孤立行", _delete_orphan_rows),
    Migration(19, "脱敏历史错误文本", _sanitize_stored_messages),
    Migration(20, "重新识别没有 TMDB 编号的 MoviePilot 下载历史缓存", _forget_torrent_media_without_tmdb),
    Migration(21, "种子识别缓存带上电影 / 剧集类型，清空旧缓存重新识别", _forget_untyped_torrent_media),
)
SCHEMA_VERSION = max(migration.version for migration in MIGRATIONS)


def pending(previous_version: int, *, before_schema: bool) -> list[Migration]:
    return [
        migration for migration in sorted(MIGRATIONS, key=lambda item: item.version)
        if migration.version > previous_version and migration.before_schema == before_schema
    ]
