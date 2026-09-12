"""Regression coverage for first-search identity and history group boundaries."""

from unittest.mock import AsyncMock, patch

from app.api.cart import _matches_active_torrent
from app.database import connect, json_value
from app.services.history import clear_download_history, projected_download_history
from app.services.search import run_search, searchable_playlist_items
from tests.support import IsolatedAppTestCase


class IdentityHistoryAuditTests(IsolatedAppTestCase):
    def create_item(self, title="The Thing", year=1982, library_state="not_found"):
        with connect() as conn:
            playlist_id = conn.execute(
                "INSERT INTO playlists(name,created_at) VALUES('Audit','2026-09-13')",
            ).lastrowid
            item_id = conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,original_title,year,library_state)
                   VALUES(?,1,?,?,?)""", (playlist_id, title, year, library_state),
            ).lastrowid
        return playlist_id, item_id

    async def test_first_search_accepts_newly_recognized_original_title(self):
        playlist_id, _ = self.create_item("星际穿越", 2014)
        with connect() as conn:
            conn.execute("INSERT INTO pt_sites(name,adapter,created_at) VALUES('Audit','torznab','2026-09-13')")
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,1,1,'queued',1,'2026-09-13','2026-09-13')""", (playlist_id,),
            ).lastrowid

        async def search_site(task, item, site, clients, semaphore, queries):
            return site, [{"title": "Interstellar.2014.1080p.BluRay.x265-ADE",
                           "site_name": "Audit", "size": 1000000000, "seeders": 10}], None, 1

        media = {"id": 157336, "title": "星际穿越", "original_title": "Interstellar", "release_date": "2014-11-07"}
        with patch("app.services.search.recognize_movie", new=AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", new=AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.search_one_site", new=search_site):
            await run_search(task_id)
        with connect() as conn:
            rows = conn.execute("SELECT eligibility,exclusion_reason FROM candidates WHERE task_id=?", (task_id,)).fetchall()
        self.assertEqual([(row["eligibility"], row["exclusion_reason"]) for row in rows], [("eligible", None)])

    async def test_library_group_clear_preserves_failed_and_uses_orphan_snapshot(self):
        for state, group in (("in_library", "organized"), ("strm", "pending_library")):
            with self.subTest(group=group):
                _, item_id = self.create_item(library_state=state)
                with connect() as conn:
                    failed_id = conn.execute(
                        """INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at)
                           VALUES(?,'Failed','Failed',0,'2026-09-13')""", (item_id,),
                    ).lastrowid
                    conn.execute(
                        """INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at)
                           VALUES(?,'Success','Success',1,'2026-09-13')""", (item_id,),
                    )
                    conn.execute(
                        """INSERT INTO download_history(playlist_item_snapshot_json,title,torrent_name,success,created_at)
                           VALUES(?,'Orphan','Orphan',1,'2026-09-13')""", (json_value({"library_state": state}),),
                    )
                with patch("app.services.history.TransmissionClient.current_downloads", new=AsyncMock(return_value=[])):
                    deleted = await clear_download_history(group)
                self.assertEqual(deleted, 2)
                with connect() as conn:
                    self.assertIsNotNone(conn.execute("SELECT 1 FROM download_history WHERE id=?", (failed_id,)).fetchone())

    async def test_remake_does_not_block_queue_cart_or_match_history(self):
        playlist_id, item_id = self.create_item()
        with connect() as conn:
            item = dict(conn.execute("SELECT * FROM playlist_items WHERE id=?", (item_id,)).fetchone())
            conn.execute(
                """INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at)
                   VALUES(?,'The Thing','The.Thing.1982.1080p.Release',1,'2026-09-13')""", (item_id,),
            )
        torrent = {"name": "The.Thing.2011.1080p.Release", "status": 4, "percentDone": 0.2}
        candidate = {"title": "The.Thing.1982.1080p.Release"}
        self.assertFalse(_matches_active_torrent(candidate, item, [torrent]))
        with patch("app.services.search.TransmissionClient.current_downloads", new=AsyncMock(return_value=[torrent])):
            queue = await searchable_playlist_items(playlist_id)
            history = await projected_download_history()
        self.assertEqual([row["id"] for row in queue["items"]], [item_id])
        self.assertNotEqual(history[0]["lifecycle_status"], "downloading")
        self.assertTrue(_matches_active_torrent(candidate, item, [{**torrent, "name": "The.Thing.1982.720p.Other"}]))

    async def test_group_clear_reaches_matches_beyond_first_page(self):
        with connect() as conn:
            conn.execute("INSERT INTO download_history(title,torrent_name,success,created_at) VALUES('Old','Old',1,'2026-09-13')")
            conn.executemany(
                "INSERT INTO download_history(title,torrent_name,success,created_at) VALUES('Failed','Failed',0,'2026-09-13')",
                [() for _ in range(5000)],
            )
        with patch("app.services.history.TransmissionClient.current_downloads", new=AsyncMock(return_value=[])):
            self.assertEqual(await clear_download_history("submitted"), 1)
