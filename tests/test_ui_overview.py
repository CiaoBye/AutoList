"""Dashboard aggregate counts must cover full history and preserve playlist scope."""

from app.api.playlists import overview
from app.database import connect
from tests.support import IsolatedAppTestCase


class OverviewUiTests(IsolatedAppTestCase):
    async def test_failure_count_covers_full_history_and_primary_playlist_is_named(self):
        with connect() as conn:
            other = conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES('Other',2,'2026-09-13')",
            ).lastrowid
            primary = conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES('Primary',1,'2026-09-13')",
            ).lastrowid
            conn.executemany(
                "INSERT INTO playlist_items(playlist_id,rank_no,original_title,tmdb_id) VALUES(?,?,?,?)",
                [(primary, 1, "Recognized", 1), (primary, 2, "Unrecognized", None), (other, 1, "Other", None)],
            )
            # Place failures outside the /api/history latest-200 response window.
            conn.executemany(
                "INSERT INTO download_history(title,torrent_name,success,created_at) VALUES('Failure','Failure',0,'2026-09-13')",
                [() for _ in range(3)],
            )
            conn.executemany(
                "INSERT INTO download_history(title,torrent_name,success,created_at) VALUES('Success','Success',1,'2026-09-13')",
                [() for _ in range(205)],
            )
        result = await overview()
        self.assertEqual(result["failed_history_count"], 3)
        self.assertEqual(result["history_count"], 208)
        self.assertEqual(result["playlist_id"], primary)
        self.assertEqual(result["playlist_name"], "Primary")
        self.assertEqual(result["item_count"], 2)
        self.assertEqual(result["recognized_count"], 1)
        self.assertEqual(result["pending_count"], result["not_in_library_count"])

    async def test_empty_overview_has_zero_failed_history(self):
        result = await overview()
        self.assertEqual(result["failed_history_count"], 0)
        self.assertEqual(result["history_count"], 0)
        self.assertIsNone(result["playlist_name"])
