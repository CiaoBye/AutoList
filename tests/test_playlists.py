"""片单：导入、来源刷新、排序、删除与自动化设置。"""

from __future__ import annotations

import asyncio
import base64
import unittest
import zipfile
from io import BytesIO
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from openpyxl import Workbook

from app.api import playlists as playlist_routes
from app.api.playlists import (
    configure_playlist_automation,
    import_playlist,
    preview_playlist_import,
    refresh_playlist_source,
    reorder_playlists,
)
from app.config import settings
from app.database import connect
from app.list_sources import validate_source_url
from app.schemas import ImportPayload, PlaylistAutomationPayload, PlaylistOrderPayload
from app.services import automation
from app.services.history import projected_download_history
from app.services.imports import MAX_XLSX_ENTRIES, parse_xlsx, resolve_import
from app.util import to_int, utc_now
from tests.support import IsolatedAppTestCase, SeededPlaylistTestCase


class PlaylistInputTests(unittest.TestCase):
    def test_playlist_source_url_rejects_embedded_secrets(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能包含"):
            validate_source_url("https://letterboxd.com/user/list/example/?token=secret")

    def test_empty_xlsx_returns_a_readable_validation_error(self) -> None:
        workbook = Workbook()
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        encoded = base64.b64encode(output.getvalue()).decode()
        with self.assertRaises(HTTPException) as raised:
            parse_xlsx(encoded)
        self.assertEqual(raised.exception.status_code, 422)


class PlaylistApiTests(SeededPlaylistTestCase):
    async def test_playlist_automation_and_sync_settings_are_explicitly_safe(self) -> None:
        automation = await configure_playlist_automation(
            self.playlist_id, PlaylistAutomationPayload(enabled=True, auto_select=False, batch_size=25),
        )
        self.assertFalse(automation["auto_download"])
        with self.assertRaises(HTTPException) as raised:
            await configure_playlist_automation(
                self.playlist_id, PlaylistAutomationPayload(enabled=True, auto_select=True, batch_size=25),
            )
        self.assertEqual(raised.exception.status_code, 422)

    async def test_source_refresh_reuses_item_ids_and_snapshots_removed_history(self) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE playlists SET source_url='https://letterboxd.com/test/list/sample/' WHERE id=?",
                (self.playlist_id,),
            )
            rows = conn.execute(
                "SELECT * FROM playlist_items WHERE playlist_id=? AND rank_no IN (1,2)", (self.playlist_id,),
            ).fetchall()
            retained_id, removed_id = to_int(rows[0]["id"]), to_int(rows[1]["id"])
            conn.execute(
                "INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at) VALUES(?,?,?,?,?)",
                (removed_id, "Movie 2", "Movie 2 2002 1080p", 1, utc_now()),
            )

        async def fetch_source(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "source_name": "刷新来源",
                "items": [{"rank_no": 1, "imdb_id": "tt0000001", "original_title": "Movie 1", "year": 2001}],
            }

        with patch("app.api.playlists.PlaylistSourceFetcher.fetch", new=fetch_source):
            result = await refresh_playlist_source(self.playlist_id)
        self.assertEqual(result["count"], 1)
        with connect() as conn:
            retained = conn.execute("SELECT id FROM playlist_items WHERE playlist_id=?", (self.playlist_id,)).fetchone()
            snapshot = conn.execute(
                "SELECT playlist_item_snapshot_json FROM download_history WHERE playlist_item_id=?",
                (removed_id,),
            ).fetchone()[0]
        self.assertEqual(to_int(retained["id"]), retained_id)
        self.assertIn("Movie 2", snapshot)
        self.assertNotEqual(retained_id, removed_id)
        with patch(
            "app.services.history.TransmissionClient.current_downloads",
            new=AsyncMock(return_value=[{"name": "Movie 2 2002 1080p", "status": 4, "percentDone": 0}]),
        ):
            history = await projected_download_history()
        removed_history = next(item for item in history if item["title"] == "Movie 2")
        self.assertEqual(removed_history["lifecycle_status"], "downloading")

    async def test_delete_playlist_preserves_history_snapshot(self) -> None:
        with connect() as conn:
            item = conn.execute(
                "SELECT * FROM playlist_items WHERE playlist_id=? AND rank_no=3", (self.playlist_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at) VALUES(?,?,?,?,?)",
                (item["id"], "Movie 3", "Movie 3 2003 1080p", 1, utc_now()),
            )
            item_id = to_int(item["id"])
        await playlist_routes.delete_playlist(self.playlist_id)
        with connect() as conn:
            snapshot = conn.execute(
                "SELECT playlist_item_snapshot_json FROM download_history WHERE playlist_item_id=?",
                (item_id,),
            ).fetchone()[0]
        self.assertIn("Movie 3", snapshot)

    async def test_source_refresh_rechecks_tasks_before_replacing_items(self) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE playlists SET source_url='https://letterboxd.com/test/list/sample/' WHERE id=?",
                (self.playlist_id,),
            )

        async def fetch_and_start_task(*_args: object, **_kwargs: object) -> dict[str, object]:
            with connect() as conn:
                conn.execute(
                    """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (self.playlist_id, 1, 1, "queued", 1, utc_now(), utc_now()),
                )
            return {
                "source_name": "竞态来源",
                "items": [{"rank_no": 1, "imdb_id": "tt9999999", "original_title": "Replacement", "year": 2025}],
            }

        with patch("app.api.playlists.PlaylistSourceFetcher.fetch", new=fetch_and_start_task):
            with self.assertRaises(HTTPException) as raised:
                await refresh_playlist_source(self.playlist_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "片单刷新期间启动了寻片任务，已保留现有片单")
        with connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (self.playlist_id,),
            ).fetchone()[0]
            first_title = conn.execute(
                "SELECT original_title FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1",
                (self.playlist_id,),
            ).fetchone()[0]
        self.assertEqual(count, 250)
        self.assertEqual(first_title, "Movie 1")

    async def test_source_refresh_is_refused_while_a_task_is_active(self) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE playlists SET source_url='https://letterboxd.com/test/list/sample/' WHERE id=?",
                (self.playlist_id,),
            )
            conn.execute(
                "INSERT INTO automation_runs(playlist_id,status,stage,trigger,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (self.playlist_id, "queued", "queued", "manual", utc_now(), utc_now()),
            )
        with patch("app.api.playlists.PlaylistSourceFetcher.fetch", new=AsyncMock()) as fetch:
            with self.assertRaises(HTTPException) as raised:
                await refresh_playlist_source(self.playlist_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "片单仍有新片处理任务运行，请完成后再刷新")
        fetch.assert_not_called()

    async def test_playlist_reorder_rejects_duplicate_ids(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await reorder_playlists(PlaylistOrderPayload(ids=[self.playlist_id, self.playlist_id]))
        self.assertEqual(raised.exception.status_code, 422)

    async def test_xlsx_too_many_entries_rejected(self) -> None:
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for index in range(MAX_XLSX_ENTRIES + 1):
                zf.writestr(f"entry-{index}.xml", "<x/>")
        encoded = base64.b64encode(archive.getvalue()).decode()
        with self.assertRaises(HTTPException) as raised:
            await resolve_import(ImportPayload(xlsx_base64=encoded))
        self.assertEqual(raised.exception.status_code, 413)


class ImportWorkflowTests(IsolatedAppTestCase):
    async def test_import_normalization_deduplicates_and_rebuilds_rank(self) -> None:
        payload = ImportPayload(json_data={"name": "混合片单", "films": [
            {"rank_no": 9, "imdb_id": "tt0000001", "original_title": "First", "year": 2001},
            {"rank_no": 20, "imdb_id": "tt0000001", "original_title": "Duplicate", "year": 2001},
            {"rank_no": 30, "tmdb_id": 222, "original_title": "Second", "year": "2002"},
            {"rank_no": 40, "original_title": "Same Title", "year": 2003},
            {"rank_no": 50, "original_title": "Same Title", "year": 2003},
            {"rank_no": 60, "original_title": "Same Title", "year": 2004},
        ]})
        name, items, source = await resolve_import(payload)
        self.assertEqual(name, "混合片单")
        self.assertEqual(source["source_type"], "json")
        self.assertEqual([item["rank_no"] for item in items], [1, 2, 3, 4])
        self.assertEqual([item["original_title"] for item in items], ["First", "Second", "Same Title", "Same Title"])

        preview = await preview_playlist_import(payload)
        imported = await import_playlist(payload)
        self.assertEqual(preview["count"], imported["count"])
        with connect() as conn:
            stored = [dict(row) for row in conn.execute(
                "SELECT rank_no,tmdb_id,source_tmdb_id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no", (imported["id"],),
            ).fetchall()]
        self.assertEqual([item["rank_no"] for item in stored], [1, 2, 3, 4])
        # 来源自带的 TMDB 编号只记为来源身份，识别结果由识别任务写入。
        self.assertEqual((stored[1]["tmdb_id"], stored[1]["source_tmdb_id"]), (None, 222))


class PlaylistSyncLockTests(IsolatedAppTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        settings.tr_base_url = ""
        settings.emby_base_url = ""
        settings.emby_api_key = ""

    async def test_playlist_sync_lock_rejects_same_playlist_overlap(self) -> None:
        playlist_id = 987654
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_sync(_playlist_id: int, _trigger: str) -> dict[str, object]:
            entered.set()
            await release.wait()
            return {"id": _playlist_id}

        with patch("app.services.automation._sync_playlist_incremental", new=blocked_sync):
            first = asyncio.create_task(automation.sync_playlist_incremental(playlist_id))
            await entered.wait()
            with self.assertRaises(HTTPException) as raised:
                await automation.sync_playlist_incremental(playlist_id)
            self.assertEqual(raised.exception.status_code, 409)
            release.set()
            self.assertEqual((await first)["id"], playlist_id)
