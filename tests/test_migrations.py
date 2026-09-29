"""数据库：编号迁移只执行一次、新库与旧库最终结构一致，以及过期数据清理。"""

from __future__ import annotations

from unittest.mock import patch

from app import migrations
from app.api import search as search_routes
from app.database import SCHEMA_VERSION, cleanup_old_data, connect, initialize
from app.util import to_int, utc_now
from tests.support import IsolatedAppTestCase, SeededPlaylistTestCase


class MigrationTests(IsolatedAppTestCase):
    def test_versions_are_unique_and_define_the_schema_version(self) -> None:
        versions = [migration.version for migration in migrations.MIGRATIONS]
        self.assertEqual(len(versions), len(set(versions)))
        self.assertEqual(versions, sorted(versions))
        self.assertEqual(SCHEMA_VERSION, max(versions))

    def test_only_steps_newer_than_the_database_run(self) -> None:
        self.assertEqual([step.version for step in migrations.pending(15, before_schema=False)], [16, 17, 18, 19])
        self.assertEqual([step.version for step in migrations.pending(12, before_schema=True)], [13])
        self.assertEqual(migrations.pending(SCHEMA_VERSION, before_schema=False), [])

    def test_initialize_runs_each_pending_step_once(self) -> None:
        with connect() as conn:
            conn.execute("PRAGMA user_version=17")
        calls: list[int] = []
        steps = tuple(
            migrations.Migration(step.version, step.description, lambda _conn, version=step.version: calls.append(version), step.before_schema)
            for step in migrations.MIGRATIONS
        )
        with patch.object(migrations, "MIGRATIONS", steps):
            initialize()
            initialize()
        self.assertEqual(calls, [18, 19])

    def test_fresh_database_has_every_added_column(self) -> None:
        with connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            for table, columns in migrations.ADDED_COLUMNS.items():
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertTrue(set(columns) <= existing, (table, set(columns) - existing))


class DatabaseMaintenanceTests(SeededPlaylistTestCase):
    async def test_database_uses_wal_and_busy_timeout(self) -> None:
        with connect() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlist_items)")}
        self.assertTrue({"emby_item_id", "emby_image_tag"}.issubset(columns))
        with connect() as conn:
            playlist_columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlists)")}
            task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(search_tasks)")}
            download_columns = {row["name"] for row in conn.execute("PRAGMA table_info(download_history)")}
            site_columns = {row["name"] for row in conn.execute("PRAGMA table_info(pt_sites)")}
        self.assertTrue({"automation_enabled", "sync_enabled", "next_sync_at"}.issubset(playlist_columns))
        self.assertTrue({"parent_task_id", "trigger", "site_ids_json", "item_ids_json", "pair_scope_json"}.issubset(task_columns))
        self.assertTrue({"playlist_item_id", "submission_hash", "playlist_item_snapshot_json", "resource_key"}.issubset(download_columns))
        self.assertTrue({
            "account_uploaded", "account_downloaded", "account_ratio",
            "account_stats_checked_at", "account_stats_error", "last_duration_ms",
        }.issubset(site_columns))

    async def test_initialize_migrates_history_resource_key_and_rejects_future_schema(self) -> None:
        item_id: int
        with connect() as conn:
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid)
            item_id = to_int(conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0])
            conn.execute(
                """INSERT INTO candidates(
                     id,task_id,playlist_item_id,candidate_index,title,site_name,size,ranking,metadata_json,resource_key,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("migration-candidate", task_id, item_id, 0, "Movie.2020.1080p", "迁移站", 1024, 1, "{}", "movie-key:16", utc_now()),
            )
            conn.execute("DROP TABLE download_history")
            conn.execute(
                """CREATE TABLE download_history(
                     id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT, playlist_item_id INTEGER,
                     playlist_item_snapshot_json TEXT, title TEXT NOT NULL, torrent_name TEXT NOT NULL,
                     site_name TEXT, submission_hash TEXT, success INTEGER NOT NULL, message TEXT, created_at TEXT NOT NULL
                   )""",
            )
            conn.execute(
                """INSERT INTO download_history(
                     candidate_id,playlist_item_id,title,torrent_name,site_name,success,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                ("migration-candidate", item_id, "Movie 1", "Movie.2020.1080p", "迁移站", 1, utc_now()),
            )
            # 缺少资源指纹的历史来自第 15 步之前的版本。
            conn.execute("PRAGMA user_version=14")
        initialize()
        with connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(download_history)")}
            history = conn.execute("SELECT resource_key FROM download_history").fetchone()
        self.assertIn("resource_key", columns)
        self.assertEqual(history["resource_key"], "movie-key:16")

        with connect() as conn:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        with self.assertRaises(RuntimeError):
            initialize()
        with connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 1)

    async def test_upgrade_renames_cart_storage_to_selection_without_losing_rows(self) -> None:
        with connect() as conn:
            # 还原成 1.61（schema v12）的命名，并放入一条已选定的资源。
            conn.execute("ALTER TABLE selection_items RENAME TO cart_items")
            conn.execute("ALTER TABLE playlists RENAME COLUMN automation_auto_select TO automation_auto_cart")
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", ("kept", utc_now()))
            conn.execute("PRAGMA user_version=12")
        initialize()
        with connect() as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlists)")}
            kept = [row["candidate_id"] for row in conn.execute("SELECT candidate_id FROM selection_items")]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertNotIn("cart_items", tables)
        self.assertEqual(kept, ["kept"])
        self.assertIn("automation_auto_select", columns)
        self.assertNotIn("automation_auto_cart", columns)
        self.assertEqual(version, SCHEMA_VERSION)

    async def test_cleanup_preserves_download_history(self) -> None:
        with connect() as conn:
            conn.execute(
                """INSERT INTO download_history(title,torrent_name,success,message,created_at)
                   VALUES(?,?,?,?,datetime('now','-365 days'))""",
                ("Old Movie", "Old Torrent", 1, None),
            )
        cleanup_old_data()
        with connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM download_history WHERE title='Old Movie'",
            ).fetchone()
        self.assertIsNotNone(exists)

    async def test_cleanup_archives_failed_search_without_deleting_candidates(self) -> None:
        with connect() as conn:
            item_id = to_int(conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1",
                (self.playlist_id,),
            ).fetchone()[0])
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(
                       playlist_id,range_start,range_end,status,total,created_at,updated_at
                   ) VALUES(?,?,?,?,?,datetime('now','-2 days'),datetime('now','-2 days'))""",
                (self.playlist_id, 1, 1, "failed", 1),
            ).lastrowid)
            conn.execute(
                """INSERT INTO candidates(
                       id,task_id,playlist_item_id,candidate_index,title,group_tier,ranking,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,datetime('now','-2 days'))""",
                ("archived-candidate", task_id, item_id, 0, "保留候选", 9, 1, "{}"),
            )
        cleanup_old_data()
        with connect() as conn:
            task = conn.execute("SELECT status FROM search_tasks WHERE id=?", (task_id,)).fetchone()
            candidate = conn.execute("SELECT 1 FROM candidates WHERE id='archived-candidate'").fetchone()
        visible = await search_routes.search_tasks(limit=50)
        self.assertEqual(task["status"], "archived")
        self.assertIsNotNone(candidate)
        self.assertNotIn(task_id, [item["id"] for item in visible])

    async def test_initialize_scrubs_legacy_history(self) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO download_history(title,torrent_name,success,message,created_at) VALUES(?,?,?,?,?)",
                ("电影", "资源", 0, "https://tracker.test/a?passkey=legacy-secret", utc_now()),
            )
            # 未脱敏的错误原文来自第 19 步之前的版本。
            conn.execute("PRAGMA user_version=18")
        initialize()
        with connect() as conn:
            message = conn.execute("SELECT message FROM download_history ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertNotIn("legacy-secret", message)
        self.assertIn("passkey=***", message)

    async def test_cleanup_archives_stale_failed_tasks_across_timestamp_formats(self) -> None:
        """cleanup_old_data 的日期比较必须兼容 ISO 'T' 与空格两种存储格式。"""
        with connect() as conn:
            now = utc_now()
            conn.executemany(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?) """,
                [
                    (self.playlist_id, 1, 1, "failed", 1, now, "2020-01-01T00:00:00+00:00"),
                    (self.playlist_id, 2, 2, "failed", 1, now, "2020-01-01 00:00:00"),
                    (self.playlist_id, 3, 3, "failed", 1, now, now),
                ],
            )
        cleanup_old_data()
        with connect() as conn:
            rows = conn.execute(
                "SELECT status FROM search_tasks WHERE playlist_id=? ORDER BY range_start", (self.playlist_id,),
            ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["archived", "archived", "failed"])
