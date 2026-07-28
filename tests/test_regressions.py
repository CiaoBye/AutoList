import base64
import gzip
import json
import tempfile
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from defusedxml import ElementTree
from defusedxml.common import EntitiesForbidden
from fastapi import HTTPException
from openpyxl import Workbook

from app import main
from app.clients import TransmissionClient
from app.config import settings
from app.database import connect, initialize
from app.list_sources import validate_source_url
from app.security import sanitize_sensitive_text


class SecurityTests(unittest.TestCase):
    def test_sensitive_values_are_redacted_in_urls_headers_and_json(self) -> None:
        message = (
            "GET https://user:password@tracker.test/download?passkey=secret&api_key=key "
            "headers={'Authorization': 'Bearer abc.def', 'Cookie': 'uid=1; token=two'}"
        )
        sanitized = sanitize_sensitive_text(message)
        for secret in ("password@", "passkey=secret", "api_key=key", "abc.def", "uid=1", "token=two"):
            self.assertNotIn(secret, sanitized)
        self.assertIn("passkey=***", sanitized)
        self.assertIn("'Cookie': '***'", sanitized)

    def test_untrusted_xml_entities_are_rejected(self) -> None:
        malicious = '<!DOCTYPE data [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><data>&xxe;</data>'
        with self.assertRaises(EntitiesForbidden):
            ElementTree.fromstring(malicious)

    def test_only_raster_magic_bytes_are_accepted_for_site_icons(self) -> None:
        self.assertEqual(main.raster_image_media_type(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertIsNone(main.raster_image_media_type(b"<svg onload='alert(1)'></svg>"))

    def test_playlist_source_url_rejects_embedded_secrets(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能包含"):
            validate_source_url("https://letterboxd.com/user/list/example/?token=secret")

    def test_cookiecloud_gzip_expansion_is_bounded(self) -> None:
        payload = gzip.compress(b"x" * 2048)
        with self.assertRaises(HTTPException) as raised:
            main.decode_cookiecloud_body(payload, "gzip", limit=1024)
        self.assertEqual(raised.exception.status_code, 413)

    def test_empty_xlsx_returns_a_readable_validation_error(self) -> None:
        workbook = Workbook()
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        encoded = base64.b64encode(output.getvalue()).decode()
        with self.assertRaises(HTTPException) as raised:
            main.parse_xlsx(encoded)
        self.assertEqual(raised.exception.status_code, 422)


class DatabaseAndApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous_data_dir = settings.data_dir
        self.previous_dashboard_random_posters = settings.dashboard_random_posters
        settings.data_dir = self.temp.name
        settings.dashboard_random_posters = False
        main.raw_candidates.clear()
        main.poster_cache.clear()
        initialize()
        with connect() as conn:
            playlist_id = conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)", ("测试片单", 1, main.utc_now()),
            ).lastrowid
            self.playlist_id = int(playlist_id)
            conn.executemany(
                """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,library_state)
                   VALUES(?,?,?,?,?,?,?)""",
                [
                    (self.playlist_id, index, f"tt{index:07d}", f"Movie {index}", 2000 + index % 20, f"电影 {index}", "in_library" if index % 2 else "unknown")
                    for index in range(1, 251)
                ],
            )

    async def asyncTearDown(self) -> None:
        main.raw_candidates.clear()
        main.poster_cache.clear()
        settings.data_dir = self.previous_data_dir
        settings.dashboard_random_posters = self.previous_dashboard_random_posters
        self.temp.cleanup()

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
        self.assertTrue({"automation_enabled", "sync_enabled", "next_sync_at"}.issubset(playlist_columns))
        self.assertTrue({"parent_task_id", "trigger", "site_ids_json", "item_ids_json"}.issubset(task_columns))
        self.assertTrue({"playlist_item_id", "submission_hash"}.issubset(download_columns))

    async def test_playlist_automation_and_sync_settings_are_explicitly_safe(self) -> None:
        automation = await main.configure_playlist_automation(
            self.playlist_id, main.PlaylistAutomationPayload(enabled=True, auto_cart=False, batch_size=25),
        )
        self.assertFalse(automation["auto_download"])
        with self.assertRaises(HTTPException) as raised:
            await main.configure_playlist_sync(
                self.playlist_id, main.PlaylistSyncPayload(enabled=True, interval_hours=24),
            )
        self.assertEqual(raised.exception.status_code, 422)

    async def test_overview_exposes_only_local_emby_poster_urls(self) -> None:
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET library_state='unknown',emby_item_id=NULL,emby_image_tag=NULL")
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE playlist_items SET library_state='in_library',emby_item_id='abc123',emby_image_tag='tag1' WHERE id=?",
                (item_id,),
            )
        result = await main.overview()
        self.assertEqual(result["recent_items"][0]["poster_url"], f"/api/playlist-items/{item_id}/poster?tag=tag1")
        self.assertNotIn("api_key", result["recent_items"][0]["poster_url"])

    async def test_history_projects_source_backed_lifecycle_states(self) -> None:
        with connect() as conn:
            items = {
                int(row["rank_no"]): row
                for row in conn.execute(
                    "SELECT id,rank_no FROM playlist_items WHERE playlist_id=? AND rank_no BETWEEN 1 AND 5",
                    (self.playlist_id,),
                ).fetchall()
            }
            conn.execute("UPDATE playlist_items SET library_state='unknown',library_checked_at=NULL WHERE id IN (?,?,?,?)", (items[2]["id"], items[3]["id"], items[4]["id"], items[5]["id"]))
            conn.execute("UPDATE playlist_items SET library_state='in_library' WHERE id=?", (items[1]["id"],))
            conn.execute("UPDATE playlist_items SET library_state='strm' WHERE id=?", (items[5]["id"],))
            conn.executemany(
                """INSERT INTO download_history(
                       playlist_item_id,title,torrent_name,site_name,submission_hash,success,message,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (items[1]["id"], "Movie 1", "Movie 1 2001 1080p", "站点一", "hash-1", 1, None, main.utc_now()),
                    (items[2]["id"], "Movie 2", "Movie 2 2002 1080p", "站点二", "hash-2", 1, None, main.utc_now()),
                    (items[3]["id"], "Movie 3", "Movie 3 2003 1080p", "站点三", "hash-3", 1, None, main.utc_now()),
                    (items[4]["id"], "Movie 4", "Movie 4 2004 1080p", "站点四", "hash-4", 0, "提交失败 passkey=secret", main.utc_now()),
                    (items[5]["id"], "Movie 5", "Movie 5 2005 1080p", "站点五", "hash-5", 1, None, main.utc_now()),
                ],
            )
        torrents = [{
            "id": 22, "name": "Movie 2 2002 1080p", "hashString": "hash-2", "status": 4,
            "percentDone": 0.24, "labels": ["MOVIEPILOT", "站点二"], "downloadDir": "/Media/Raw/Film",
        }]
        with patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=torrents)):
            result = await main.history()
        by_title = {item["title"]: item for item in result}
        self.assertEqual(by_title["Movie 1"]["lifecycle_status"], "organized")
        self.assertEqual(by_title["Movie 1"]["status_label"], "已整理/已入库")
        self.assertEqual(by_title["Movie 1"]["status_source"], "Emby")
        self.assertEqual(by_title["Movie 2"]["lifecycle_status"], "downloading")
        self.assertEqual(by_title["Movie 2"]["status_label"], "下载中")
        self.assertEqual(by_title["Movie 2"]["status_source"], "Transmission")
        self.assertEqual(by_title["Movie 3"]["lifecycle_status"], "submitted")
        self.assertEqual(by_title["Movie 3"]["status_label"], "已提交")
        self.assertEqual(by_title["Movie 3"]["status_source"], "MoviePilot")
        self.assertEqual(by_title["Movie 4"]["lifecycle_status"], "failed")
        self.assertEqual(by_title["Movie 4"]["status_label"], "失败")
        self.assertNotIn("secret", json.dumps(by_title["Movie 4"], ensure_ascii=False))
        self.assertEqual(by_title["Movie 5"]["lifecycle_status"], "pending_library")
        self.assertEqual(by_title["Movie 5"]["status_label"], "待入库")

    async def test_overview_random_posters_are_stable_and_not_rank_ordered(self) -> None:
        ordered = await main.overview()
        self.assertEqual([item["rank_no"] for item in ordered["recent_items"]], [1, 2, 3, 4, 5, 6])
        settings.dashboard_random_posters = True
        first = await main.overview()
        second = await main.overview()
        first_ranks = [item["rank_no"] for item in first["recent_items"]]
        self.assertEqual(first_ranks, [item["rank_no"] for item in second["recent_items"]])
        self.assertEqual(len(first_ranks), 6)
        self.assertTrue(all(rank % 2 for rank in first_ranks))
        self.assertNotEqual(first_ranks, [1, 2, 3, 4, 5, 6])

    async def test_emby_poster_is_proxied_and_validated(self) -> None:
        with connect() as conn:
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE playlist_items SET emby_item_id='abc123',emby_image_tag='tag1' WHERE id=?", (item_id,),
            )
        original = main.EmbyClient.poster

        async def fake_poster(_client: object, _item_id: str) -> tuple[bytes, str]:
            return b"\x89PNG\r\n\x1a\nposter", "image/png"

        main.EmbyClient.poster = fake_poster
        try:
            response = await main.playlist_item_poster(int(item_id), "tag1")
        finally:
            main.EmbyClient.poster = original
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.body, b"\x89PNG\r\n\x1a\nposter")

    async def test_playlist_items_are_server_paginated_and_filtered(self) -> None:
        result = await main.playlist_items(self.playlist_id, page=2, page_size=100)
        self.assertEqual(result["total"], 250)
        self.assertEqual(result["page"], 2)
        self.assertEqual(len(result["items"]), 100)
        self.assertEqual(result["items"][0]["rank_no"], 101)
        filtered = await main.playlist_items(self.playlist_id, page=1, page_size=200, query="Movie 25")
        self.assertGreater(filtered["total"], 0)
        self.assertTrue(all("Movie 25" in item["original_title"] for item in filtered["items"]))
        capped = await main.playlist_items(self.playlist_id, page=1, page_size=10_000)
        self.assertEqual(capped["page_size"], 200)

    async def test_expired_cart_context_is_visible_and_recorded_safely(self) -> None:
        with connect() as conn:
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,size,group_tier,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("expired", task_id, item_id, 0, "Example 1080p", "Test", 1024, 9, 1, "{}", main.utc_now()),
            )
            conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", ("expired", main.utc_now()))
        items = await main.cart()
        self.assertFalse(items[0]["context_available"])
        with self.assertRaises(HTTPException) as raised:
            await main.download_cart()
        self.assertEqual(raised.exception.status_code, 409)
        with connect() as conn:
            history = conn.execute("SELECT success,message FROM download_history WHERE candidate_id='expired'").fetchall()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["success"], 0)
        self.assertNotIn("http", history[0]["message"].lower())

    async def test_initialize_scrubs_legacy_history(self) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO download_history(title,torrent_name,success,message,created_at) VALUES(?,?,?,?,?)",
                ("电影", "资源", 0, "https://tracker.test/a?passkey=legacy-secret", main.utc_now()),
            )
        initialize()
        with connect() as conn:
            message = conn.execute("SELECT message FROM download_history ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertNotIn("legacy-secret", message)
        self.assertIn("passkey=***", message)

    async def test_playlist_reorder_rejects_duplicate_ids(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await main.reorder_playlists(main.PlaylistOrderPayload(ids=[self.playlist_id, self.playlist_id]))
        self.assertEqual(raised.exception.status_code, 422)

    async def test_candidates_tolerate_corrupted_legacy_metadata(self) -> None:
        with connect() as conn:
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,group_tier,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("damaged", task_id, item_id, 0, "Example 1080p", 9, 1, "not-json", main.utc_now()),
            )
        result = await main.candidates(int(task_id))
        self.assertEqual(result[0]["metadata"], {})

    async def test_site_icon_blocks_cross_host_private_networks(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "内网"):
            await main.validate_remote_icon_url("http://127.0.0.1/icon.png", "https://example.com")
        await main.validate_remote_icon_url("http://127.0.0.1/icon.png", "http://127.0.0.1")

    async def test_transmission_allows_an_unauthenticated_rpc(self) -> None:
        previous = (settings.tr_base_url, settings.tr_username, settings.tr_password)
        try:
            settings.tr_base_url = "http://127.0.0.1:9091"
            settings.tr_username = ""
            settings.tr_password = ""
            client = TransmissionClient()
            self.assertIsNone(client.auth)
            self.assertTrue(client.base_url.endswith("/transmission/rpc"))
        finally:
            settings.tr_base_url, settings.tr_username, settings.tr_password = previous

    async def test_searchable_queue_excludes_transmission_downloads_and_limits_front(self) -> None:
        with patch.object(
            main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[
                {"name": "Movie 2 2002 1080p", "status": 4, "percentDone": 0.25},
            ]),
        ):
            queue = await main.searchable_playlist_items(self.playlist_id, limit=50)
        self.assertEqual(queue["total_count"], 250)
        self.assertEqual(queue["in_library_count"], 125)
        self.assertEqual(queue["downloading_count"], 1)
        self.assertEqual(queue["pending_count"], 124)
        self.assertEqual(len(queue["items"]), 50)
        self.assertNotIn(2, [item["rank_no"] for item in queue["items"]])
        self.assertEqual([item["rank_no"] for item in queue["items"][:3]], [4, 6, 8])

    async def test_pending_search_task_persists_selected_item_ids(self) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,created_at) VALUES(?,?,?,?)",
                ("测试站点", "rss", "https://example.com/feed", main.utc_now()),
            )
        with patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])), \
             patch.object(main, "run_search", new=AsyncMock()):
            result = await main.create_task(main.TaskPayload(playlist_id=self.playlist_id, scope="pending", count=50))
            await main.running_tasks[result["id"]]
            main.running_tasks.pop(result["id"], None)
        with connect() as conn:
            task = conn.execute("SELECT * FROM search_tasks WHERE id=?", (result["id"],)).fetchone()
        selected = json.loads(task["item_ids_json"])
        self.assertEqual(result["total"], 50)
        self.assertEqual(len(selected), 50)
        self.assertEqual(task["trigger"], "pending")


if __name__ == "__main__":
    unittest.main()
