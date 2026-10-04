"""统一同步：Transmission、MoviePilot、Emby 与片单影片对齐。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.database import connect
from app.services import sync, sync_events
from app.util import utc_now
from tests.support import IsolatedAppTestCase


class SyncTests(IsolatedAppTestCase):
    def setUp(self) -> None:
        super().setUp()
        sync._inflight = None
        sync._last_started = None
        sync._last_result = None
        patches = [
            patch.object(settings, "mp_base_url", "http://mp"), patch.object(settings, "mp_api_key", "k"),
            patch.object(settings, "emby_base_url", "http://emby"), patch.object(settings, "emby_api_key", "k"),
            patch("app.services.sync.TransmissionClient", return_value=type("T", (), {"base_url": "http://tr"})()),
            patch("app.services.sync._current_downloads_cached", new=AsyncMock(return_value=([{"hashString": "a" * 40}], "known_present"))),
            patch("app.services.sync.MoviePilotClient.transfer_history", new=AsyncMock(return_value=[])),
            patch("app.services.sync.EmbyClient.check", new=AsyncMock(return_value={"ok": True})),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_reconcile_reports_every_source_and_identifies_unknown_torrents(self) -> None:
        identify = AsyncMock()
        with patch("app.services.sync.identify_torrents", new=identify), \
             patch("app.services.sync.recheck_library_states", new=AsyncMock(return_value=0)):
            result = await sync.reconcile()
        self.assertEqual({name: source["ok"] for name, source in result["sources"].items()},
                         {"transmission": True, "moviepilot": True, "emby": True})
        identify.assert_awaited_once()
        self.assertEqual(sync.status()["last"]["ran_at"], result["ran_at"])
        self.assertFalse(sync.status()["running"])

    async def test_an_unreachable_source_is_reported_without_failing_the_sync(self) -> None:
        with patch("app.services.sync.EmbyClient.check", new=AsyncMock(side_effect=RuntimeError("连接被拒绝"))), \
             patch("app.services.sync.identify_torrents", new=AsyncMock()), \
             patch("app.services.sync.recheck_library_states", new=AsyncMock(return_value=0)) as recheck:
            result = await sync.reconcile()
        self.assertFalse(result["sources"]["emby"]["ok"])
        self.assertIn("Emby", result["sources"]["emby"]["message"])
        self.assertTrue(result["sources"]["transmission"]["ok"])
        recheck.assert_not_awaited()

    async def test_concurrent_requests_share_one_run(self) -> None:
        calls = 0

        async def slow_identify(_torrents: object) -> None:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)

        with patch("app.services.sync.identify_torrents", new=slow_identify), \
             patch("app.services.sync.recheck_library_states", new=AsyncMock(return_value=0)):
            first, second = await asyncio.gather(sync.reconcile(), sync.reconcile())
        self.assertEqual(calls, 1)
        self.assertEqual(first["ran_at"], second["ran_at"])
        self.assertFalse(sync.sync_due())


MP_ADDED = {
    "type": "download.added",
    "data": {"hash": "a" * 40, "downloader": "TR", "context": {"media_info": {"tmdb_id": 50, "title": "篮球梦"}}},
}


class SyncEventParseTests(unittest.TestCase):
    def test_moviepilot_download_added_carries_hash_and_tmdb_id(self) -> None:
        hint = sync_events.parse("moviepilot", json.dumps(MP_ADDED).encode(), {})
        self.assertEqual((hint.kind, hint.hash, hint.tmdb_id), ("added", "a" * 40, 50))

    def test_moviepilot_transfer_complete_uses_download_hash_and_mediainfo(self) -> None:
        body = {"type": "transfer.complete", "data": {"download_hash": "B" * 40, "mediainfo": {"tmdb_id": "51"}}}
        hint = sync_events.parse("moviepilot", json.dumps(body).encode(), {})
        self.assertEqual((hint.kind, hint.hash, hint.tmdb_id), ("organized", "b" * 40, 51))

    def test_unrelated_moviepilot_events_are_ignored(self) -> None:
        self.assertIsNone(sync_events.parse("moviepilot", json.dumps({"type": "site.updated", "data": {}}).encode(), {}))

    def test_transmission_script_posts_a_form_or_uses_the_query(self) -> None:
        self.assertEqual(sync_events.parse("transmission", f"event=done&hash={'c' * 40}&name=x".encode(), {}).kind, "done")
        hint = sync_events.parse("transmission", b"", {"event": "added", "hash": "d" * 40})
        self.assertEqual((hint.kind, hint.hash), ("added", "d" * 40))
        self.assertIsNone(sync_events.parse("transmission", b"event=added&hash=zzz", {}).hash)

    def test_emby_library_new_for_a_movie_gives_the_tmdb_id(self) -> None:
        body = {"Event": "library.new", "Item": {"Type": "Movie", "Name": "x", "ProviderIds": {"Tmdb": "123678"}}}
        hint = sync_events.parse("emby", json.dumps(body).encode(), {})
        self.assertEqual((hint.kind, hint.tmdb_id), ("library_new", 123678))
        episode = {"Event": "library.new", "Item": {"Type": "Episode", "ProviderIds": {"Tmdb": "1"}}}
        self.assertIsNone(sync_events.parse("emby", json.dumps(episode).encode(), {}))
        self.assertIsNone(sync_events.parse("emby", json.dumps({"Event": "playback.start", "Item": {}}).encode(), {}))


class EmbyEventBurstTests(IsolatedAppTestCase):
    async def test_a_burst_of_emby_events_runs_one_merged_recheck(self) -> None:
        recheck = AsyncMock(return_value=0)
        with patch.object(sync_events, "EVENT_DEBOUNCE_SECONDS", 0.05), patch.object(sync_events, "recheck_library_states", recheck):
            for _ in range(25):
                sync_events.handle(sync_events.Hint("emby", "library_new", None, 7))
            sync_events.handle(sync_events.Hint("emby", "library_new", None, 8))
            self.assertEqual(len([task for task in sync._background if not task.done()]), 1)
            await asyncio.sleep(0.3)
        self.assertEqual(recheck.await_count, 1)


class SyncEventEndpointTests(IsolatedAppTestCase):
    def setUp(self) -> None:
        super().setUp()
        sync_events._failures.clear()

    def _post(self, source: str, token: str | None, body: dict[str, object]) -> object:
        from fastapi.testclient import TestClient

        from app.main import app

        suffix = f"?token={token}" if token else ""
        with TestClient(app) as client:
            return client.post(f"/api/sync/events/{source}{suffix}", json=body)

    async def test_events_need_the_secret_and_work_without_the_access_token(self) -> None:
        token = sync_events.sync_token()
        self.assertEqual(sync_events.sync_token(), token)
        with patch.dict(os.environ, {"AUTOLIST_ACCESS_TOKEN": "x" * 40}), \
             patch("app.services.sync_events.handle") as handle:
            self.assertEqual(self._post("moviepilot", None, MP_ADDED).status_code, 401)
            self.assertEqual(self._post("moviepilot", "wrong", MP_ADDED).status_code, 401)
            self.assertEqual(self._post("unknown", token, MP_ADDED).status_code, 404)
            handle.assert_not_called()
            ok = self._post("moviepilot", token, MP_ADDED)
            self.assertEqual((ok.status_code, ok.json()), (200, {"accepted": True}))
            handle.assert_called_once()
            ignored = self._post("moviepilot", token, {"type": "site.updated", "data": {}})
            self.assertEqual(ignored.json(), {"accepted": False})

    async def test_repeated_wrong_secrets_are_rate_limited(self) -> None:
        sync_events.sync_token()
        for _ in range(sync_events.TOKEN_FAILURE_LIMIT):
            self.assertEqual(self._post("emby", "wrong", {}).status_code, 401)
        # 之后即使密钥正确，同一客户端在窗口内也会被拒绝。
        self.assertEqual(self._post("emby", settings.sync_token, {"Event": "library.new"}).status_code, 401)

    async def test_secret_is_listed_for_setup_but_never_in_public_settings(self) -> None:
        from app.api.sync import sync_webhooks

        paths = (await sync_webhooks())["paths"]
        self.assertEqual(set(paths), {"moviepilot", "transmission", "emby"})
        self.assertTrue(all(settings.sync_token in path for path in paths.values()))
        self.assertNotIn("sync_token", settings.public_values())


    async def test_reset_invalidates_the_old_secret_and_reports_transmission_hooks(self) -> None:
        from app.api.sync import reset_sync_token, sync_webhooks

        old = sync_events.sync_token()
        hooks = {"added": "/config/autolist-added.sh", "done": ""}
        with patch("app.api.sync.TransmissionClient.script_hooks", new=AsyncMock(return_value=hooks)):
            fresh = await reset_sync_token()
            listed = await sync_webhooks()
        self.assertEqual(fresh["transmission_hooks"], hooks)
        self.assertNotEqual(settings.sync_token, old)
        self.assertEqual(listed["paths"], fresh["paths"])
        self.assertFalse(sync_events.authorized(old, "1.2.3.4"))
        self.assertTrue(sync_events.authorized(settings.sync_token, "1.2.3.4"))


class SyncEventHandlingTests(IsolatedAppTestCase):
    def setUp(self) -> None:
        super().setUp()
        sync._inflight = None
        sync_events._pending = None

    async def test_moviepilot_added_remembers_the_film_on_the_torrent_and_schedules_one_sync(self) -> None:
        hint = sync_events.Hint("moviepilot", "added", "a" * 40, 50)
        with patch("app.services.sync_events.EVENT_DEBOUNCE_SECONDS", 0.01), \
             patch("app.services.sync_events.sync.reconcile", new=AsyncMock(return_value={})) as reconcile:
            sync_events.handle(hint)
            sync_events.handle(hint)  # 紧接着的第二个事件合并进同一次同步
            await sync_events._pending
        reconcile.assert_awaited_once()
        with connect() as conn:
            row = conn.execute("SELECT tmdb_id,source FROM torrent_media WHERE hash=?", ("a" * 40,)).fetchone()
        self.assertEqual((row["tmdb_id"], row["source"]), (50, "moviepilot"))
        self.assertEqual(len(sync_events.stats()["recent"]), 2)

    async def test_emby_library_new_rechecks_only_the_matching_films(self) -> None:
        with connect() as conn:
            playlist = conn.execute("INSERT INTO playlists(name,created_at) VALUES('P',?)", (utc_now(),)).lastrowid
            mine = conn.execute(
                "INSERT INTO playlist_items(playlist_id,rank_no,original_title,tmdb_id,library_state) VALUES(?,1,'A',50,'not_found')",
                (playlist,),
            ).lastrowid
            conn.execute(
                "INSERT INTO playlist_items(playlist_id,rank_no,original_title,tmdb_id,library_state) VALUES(?,2,'B',51,'not_found')",
                (playlist,),
            )
        with patch("app.services.sync_events.recheck_library_states", new=AsyncMock(return_value=1)) as recheck, \
             patch("app.services.sync_events.sync.reconcile", new=AsyncMock()) as reconcile:
            sync_events.handle(sync_events.Hint("emby", "library_new", None, 50))
            await asyncio.gather(*list(sync._background))
        recheck.assert_awaited_once_with([mine])
        reconcile.assert_not_awaited()


class AccessLogRedactionTests(unittest.TestCase):
    def test_event_secret_never_reaches_the_access_log(self) -> None:
        from app.logs import RedactTokenFilter

        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
            ("172.18.0.2:1", "POST", "/api/sync/events/emby?token=abc123&x=1", "1.1", 200), None,
        )
        RedactTokenFilter().filter(record)
        self.assertIn("token=***&x=1", record.getMessage())
        self.assertNotIn("abc123", record.getMessage())


class SyncEventStatsTests(IsolatedAppTestCase):
    async def test_events_are_kept_in_the_database_and_pruned_after_30_days(self) -> None:
        from app.database import cleanup_old_data

        with connect() as conn:
            conn.execute(
                "INSERT INTO sync_events(source,kind,received_at) VALUES('emby','library_new','2020-01-01T00:00:00+00:00')",
            )
        sync_events._record(sync_events.Hint("transmission", "added", "a" * 40))
        sync_events._record(sync_events.Hint("emby", "library_new", None, 50))
        stats = sync_events.stats()
        self.assertEqual(stats["events"]["emby"]["count"], 2)
        self.assertEqual((stats["events"]["emby"]["last_kind"], stats["events"]["moviepilot"]["count"]), ("library_new", 0))
        self.assertEqual(stats["recent"][0]["tmdb_id"], 50)
        self.assertEqual(stats["recent"][1]["hash"], "aaaaaaaa")
        cleanup_old_data()
        self.assertEqual(sync_events.stats()["events"]["emby"]["count"], 1)


class SiteRetestTests(IsolatedAppTestCase):
    def _site(self, name: str, status: str, tested: str, search: int = 1) -> int:
        with connect() as conn:
            return int(conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,last_status,last_tested_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (name, "nexusphp", f"https://{name}.example", 1, search, status, tested, utc_now()),
            ).lastrowid)

    async def test_only_stale_failing_searchable_sites_are_retested(self) -> None:
        old = "2020-01-01T00:00:00+00:00"
        failing = self._site("failing", "error", old)
        self._site("fine", "ok", old)
        self._site("fresh", "error", utc_now())
        self._site("not-searched", "error", old, search=0)
        retest = AsyncMock(return_value={"ok": True})
        with patch("app.services.sync.test_site_config", new=retest):
            recovered = await sync.retest_failing_sites()
        self.assertEqual(recovered, 1)
        self.assertEqual([call.args[0]["id"] for call in retest.await_args_list], [failing])

    async def test_nothing_is_retested_while_a_search_is_running(self) -> None:
        self._site("failing", "error", "2020-01-01T00:00:00+00:00")
        retest = AsyncMock(return_value={"ok": True})
        with patch("app.services.sync.test_site_config", new=retest), \
             patch("app.services.sync.SEARCH.active_count", return_value=1):
            self.assertEqual(await sync.retest_failing_sites(), 0)
        retest.assert_not_awaited()


class StalledRemovalTests(IsolatedAppTestCase):
    async def test_replaced_stalled_torrents_are_removed_and_files_kept_for_the_same_release(self) -> None:
        entries = [
            {"item_id": 1, "hash": "a" * 40, "name": "Old.Release", "keep_data": False},
            {"item_id": 1, "hash": "b" * 40, "name": "Same.Release", "keep_data": True},
        ]
        remove = AsyncMock()
        with patch("app.services.sync.replaced_stalled_torrents", new=AsyncMock(return_value=entries)), \
             patch("app.services.sync.TransmissionClient.remove_torrents", new=remove):
            self.assertEqual(await sync.remove_replaced_stalled([]), 2)
        calls = {tuple(call.args[0]): call.kwargs["delete_data"] for call in remove.await_args_list}
        self.assertEqual(calls, {("b" * 40,): False, ("a" * 40,): True})

    async def test_nothing_is_removed_when_nothing_was_replaced_or_the_remove_fails(self) -> None:
        remove = AsyncMock()
        with patch("app.services.sync.replaced_stalled_torrents", new=AsyncMock(return_value=[])), \
             patch("app.services.sync.TransmissionClient.remove_torrents", new=remove):
            self.assertEqual(await sync.remove_replaced_stalled([]), 0)
        remove.assert_not_awaited()
        entries = [{"item_id": 1, "hash": "a" * 40, "name": "Old", "keep_data": False}]
        with patch("app.services.sync.replaced_stalled_torrents", new=AsyncMock(return_value=entries)), \
             patch("app.services.sync.TransmissionClient.remove_torrents", new=AsyncMock(side_effect=RuntimeError("拒绝"))):
            self.assertEqual(await sync.remove_replaced_stalled([]), 0)


class StalledResearchTests(IsolatedAppTestCase):
    def _film(self) -> dict:
        with connect() as conn:
            playlist_id = conn.execute("INSERT INTO playlists(name,created_at) VALUES('P',?)", (utc_now(),)).lastrowid
            item_id = conn.execute(
                "INSERT INTO playlist_items(playlist_id,rank_no,original_title,year,library_state) VALUES(?,1,'Stalled Film',2020,'not_found')",
                (playlist_id,),
            ).lastrowid
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at) VALUES('站点','nexusphp','https://s.example',1,1,?)",
                (utc_now(),),
            )
            row = conn.execute("SELECT * FROM playlist_items WHERE id=?", (item_id,)).fetchone()
        return dict(row)

    async def test_stalled_film_without_a_usable_candidate_is_searched_again_once(self) -> None:
        item = self._film()
        projected = [{"id": item["id"], "status": "downloading", "issues": ["download_stalled"]}]
        started: list[object] = []

        async def fake_search(task_id: int) -> None:
            return None

        with patch.object(sync, "run_search", fake_search), patch.object(sync.SEARCH, "start", lambda task_id, work: (started.append(task_id), work.close())):
            self.assertEqual(await sync.research_stalled(projected, [item]), 1)
            # 刚启动过：冷却期内不再重复，也不会因为任务还没跑完而叠加。
            self.assertEqual(await sync.research_stalled(projected, [item]), 0)
        self.assertEqual(len(started), 1)
        with connect() as conn:
            row = conn.execute("SELECT trigger,item_ids_json FROM search_tasks").fetchone()
        self.assertEqual((row["trigger"], json.loads(row["item_ids_json"])), ("stalled", [item["id"]]))

    async def test_a_healthy_download_is_left_alone(self) -> None:
        item = self._film()
        projected = [{"id": item["id"], "status": "downloading", "issues": []}]
        self.assertEqual(await sync.research_stalled(projected, [item]), 0)
        with connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_tasks").fetchone()[0], 0)


class EnrichTorrentMediaTests(IsolatedAppTestCase):
    async def test_a_recognised_torrent_without_title_or_poster_is_filled_from_tmdb_once(self) -> None:
        from app.services import downloads

        downloads._enrich_tried.clear()
        with connect() as conn:
            conn.execute(
                "INSERT INTO torrent_media(hash,tmdb_id,source,checked_at,media_type) VALUES(?,?,?,?,'movie')", ("a" * 40, 1144107, "moviepilot", utc_now()),
            )
        details = AsyncMock(return_value={"title": "罗小黑战记 2", "release_date": "2025-09-12", "poster_path": "/abc.jpg"})
        with patch.object(settings, "tmdb_api_key", "k"), patch("app.services.downloads.TMDBClient") as client:
            client.return_value.movie_details = details
            self.assertEqual(await downloads.enrich_torrent_media(["a" * 40]), 1)
            self.assertEqual(await downloads.enrich_torrent_media(["a" * 40]), 0)
        with connect() as conn:
            row = conn.execute("SELECT title,year,poster_path FROM torrent_media").fetchone()
        self.assertEqual((row["title"], row["year"], row["poster_path"]), ("罗小黑战记 2", 2025, "/abc.jpg"))
        self.assertEqual(details.await_count, 1)

    async def test_a_series_is_filled_from_the_tv_endpoint_not_the_movie_one(self) -> None:
        from app.services import downloads

        downloads._enrich_tried.clear()
        with connect() as conn:
            conn.execute(
                "INSERT INTO torrent_media(hash,tmdb_id,source,checked_at,media_type) VALUES(?,?,?,?,'tv')", ("b" * 40, 1398, "moviepilot", utc_now()),
            )
        movie = AsyncMock(return_value={"title": "潜行者"})
        tv = AsyncMock(return_value={"name": "黑道家族", "first_air_date": "1999-01-10", "poster_path": "/s.jpg"})
        with patch.object(settings, "tmdb_api_key", "k"), patch("app.services.downloads.TMDBClient") as client:
            client.return_value.movie_details = movie
            client.return_value.tv_details = tv
            await downloads.enrich_torrent_media(["b" * 40])
        with connect() as conn:
            row = conn.execute("SELECT title,year,media_type FROM torrent_media").fetchone()
        self.assertEqual((row["title"], row["year"], row["media_type"]), ("黑道家族", 1999, "tv"))
        self.assertEqual((movie.await_count, tv.await_count), (0, 1))

    def test_an_event_that_only_knows_the_id_keeps_what_the_history_already_said(self) -> None:
        from app.queries import downloads as queries

        with connect() as conn:
            queries.remember_torrent_media(conn, "c" * 40, title="绝命毒师", year=2008, tmdb_id=1396, poster_path="/p.jpg", source="moviepilot", media_type="tv")
            queries.remember_torrent_media(conn, "c" * 40, title=None, year=None, tmdb_id=1396, poster_path=None, source="moviepilot")
            row = conn.execute("SELECT title,year,poster_path,media_type FROM torrent_media").fetchone()
            self.assertEqual((row["title"], row["year"], row["poster_path"], row["media_type"]), ("绝命毒师", 2008, "/p.jpg", "tv"))
            # 换了 TMDB 编号就是另一部影片：整条替换，不留旧片名。
            queries.remember_torrent_media(conn, "c" * 40, title=None, year=None, tmdb_id=550, poster_path=None, source="moviepilot", media_type="movie")
            row = conn.execute("SELECT title,tmdb_id,media_type FROM torrent_media").fetchone()
            self.assertEqual((row["title"], row["tmdb_id"], row["media_type"]), (None, 550, "movie"))
