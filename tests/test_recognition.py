"""识别：TMDB / IMDb / AI 匹配、识别任务与 Emby 入馆状态刷新。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

from app.clients import AIRecognitionClient, TMDBClient
from app.config import settings
from app.database import connect
from app.schemas import ImportPayload
from app.services.library import library_details
from app.services.recognition import recognize_movie
from app.util import to_int, utc_now
from tests.support import IsolatedAppTestCase

CURE_ZH_RESULTS = [
    {"id": 6715, "title": "鳄鱼波鞋走天涯", "original_title": "The Cure", "original_language": "en",
     "release_date": "1995-04-21", "vote_count": 368},
    {"id": 1199410, "title": "Say It, Fight It, Cure It", "original_title": "Say It, Fight It, Cure It",
     "original_language": "en", "release_date": "1997-10-05", "vote_count": 1},
    {"id": 36095, "title": "X圣治", "original_title": "キュア", "original_language": "ja",
     "release_date": "1997-12-27", "vote_count": 881},
]


class TmdbToleranceTests(unittest.TestCase):
    def test_tmdb_cross_language_and_year_tolerance_match(self) -> None:
        from app.services.recognition import select_tmdb_match
        # 1. 跨语言匹配（英文译名匹配非英语原片）
        seven_samurai_opts = [{
            "id": 346, "title": "七武士", "original_title": "七人の侍",
            "original_language": "ja", "release_date": "1954-04-26",
        }]
        match = select_tmdb_match(seven_samurai_opts, "Seven Samurai", 1954)
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 346)

        # 2. 跨年份公映首映容差（Casablanca 1942 vs 1943）
        casablanca_opts = [{
            "id": 289, "title": "卡萨布兰卡", "original_title": "Casablanca",
            "original_language": "en", "release_date": "1943-01-15",
        }]
        match = select_tmdb_match(casablanca_opts, "Casablanca", 1942)
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 289)

        # 3. 超长片名与短片名包含测试 (M -> M就是凶手)
        m_opts = [{
            "id": 832, "title": "M就是凶手", "original_title": "M - Eine Stadt sucht einen Mörder",
            "original_language": "de", "release_date": "1931-05-11",
        }]
        match = select_tmdb_match(m_opts, "M", 1931)
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 832)


class RecognitionWorkflowTests(IsolatedAppTestCase):
    async def test_recognition_prefers_imdb_and_uses_ai_only_after_tmdb_miss(self) -> None:
        imdb_match = {"id": 101, "title": "Exact", "original_title": "Exact", "release_date": "2001-01-01"}
        tmdb = AsyncMock()
        tmdb.find_by_imdb.return_value = [imdb_match]
        with patch("app.services.recognition.TMDBClient", return_value=tmdb), patch("app.services.recognition.AIRecognitionClient") as ai_type:
            result = await recognize_movie("Exact", 2001, "tt0000001")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 101)
        tmdb.search_movie.assert_not_awaited()
        ai_type.return_value.suggest.assert_not_called()

        tmdb = AsyncMock()
        tmdb.find_by_imdb.return_value = []
        # 本地化搜索 → 英文搜索都没有结果，才交给 AI 纠正片名后再搜一次。
        tmdb.search_movie.side_effect = [[], [], [{"id": 202, "title": "Corrected", "original_title": "Corrected", "release_date": "2002-02-02"}]]
        ai = AsyncMock()
        ai.suggest.return_value = {"original_title": "Corrected", "year": 2002}
        with patch("app.services.recognition.TMDBClient", return_value=tmdb), patch("app.services.recognition.AIRecognitionClient", return_value=ai):
            result = await recognize_movie("Wrong", 2002, "tt0000002")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 202)
        self.assertEqual(tmdb.search_movie.await_count, 3)
        ai.suggest.assert_awaited_once_with("Wrong", 2002)

    async def test_library_details_preserves_entity_strm_and_failure_states(self) -> None:
        emby = AsyncMock()
        emby.library_match.side_effect = [
            ("in_library", {"Id": "entity", "ImageTags": {"Primary": "p1"}}),
            ("strm", {"Id": "stream", "ImageTags": {"Primary": "p2"}}),
            RuntimeError("Emby unavailable"),
        ]
        self.assertEqual(await library_details(emby, "A", 2000), ("in_library", "entity", "p1"))
        self.assertEqual(await library_details(emby, "B", 2001), ("strm", "stream", "p2"))
        self.assertEqual(await library_details(emby, "C", 2002), ("unknown", None, None))


class RecognitionAndLibraryTaskTests(IsolatedAppTestCase):
    """P1-2：run_recognition / run_library_scan 行为测试（审计 finding P1-2）。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # 识别任务在未配置 TMDB 时会直接失败；这些用例模拟的是已配置 TMDB 的识别流程。
        settings.tmdb_api_key = "test-tmdb-key"

    def _playlist_and_items(self, count: int = 1) -> tuple[int, list[int]]:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("后台任务测试", 1, utc_now()),
            ).lastrowid)
            item_ids = []
            for rank in range(1, count + 1):
                item_ids.append(to_int(conn.execute(
                    """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title)
                       VALUES(?,?,?,?,?,?)""",
                    (playlist_id, rank, f"tt{rank:07d}", f"Movie {rank}", 2020 + rank, f"电影 {rank}"),
                ).lastrowid))
        return playlist_id, item_ids

    async def test_run_recognition_completes_and_persists(self) -> None:
        playlist_id, item_ids = self._playlist_and_items(2)
        from app.services import automation
        with connect() as conn:
            task_id = to_int(conn.execute(
                """INSERT INTO recognition_tasks(playlist_id,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "queued", 2, utc_now(), utc_now()),
            ).lastrowid)
        media = {"id": 42, "title": "Movie 1", "original_title": "Movie 1", "imdb_id": "tt0000001", "release_date": "2021-05-01"}
        with patch.object(automation, "recognize_item", new=AsyncMock(return_value=media)) as recognize, \
             patch.object(automation, "persist_tmdb_item", new=Mock()) as persist:
            await automation.run_recognition(task_id)
        self.assertEqual(recognize.await_count, 2)
        self.assertEqual(persist.call_count, 2)
        with connect() as conn:
            task = conn.execute("SELECT * FROM recognition_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["matched"], 2)
        self.assertEqual(task["completed"], 2)
        self.assertIsNone(task["error_message"])
        # 登记表已清理
        from app import tasks
        self.assertNotIn(task_id, tasks.RECOGNITION.running)

    async def test_run_recognition_marks_partial_when_item_fails(self) -> None:
        playlist_id, _item_ids = self._playlist_and_items(2)
        from app.services import automation
        with connect() as conn:
            task_id = to_int(conn.execute(
                """INSERT INTO recognition_tasks(playlist_id,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "queued", 2, utc_now(), utc_now()),
            ).lastrowid)
        media = {"id": 42, "title": "Movie 1", "original_title": "Movie 1", "imdb_id": "tt0000001", "release_date": "2021-05-01"}
        with patch.object(automation, "recognize_item", new=AsyncMock(side_effect=[media, RuntimeError("识别失败")])), \
             patch.object(automation, "persist_tmdb_item", new=Mock()):
            await automation.run_recognition(task_id)
        with connect() as conn:
            task = conn.execute("SELECT * FROM recognition_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["matched"], 1)
        self.assertEqual(task["completed"], 2)
        self.assertIn("识别失败", task["error_message"] or "")

    async def test_run_recognition_marks_cancelled_when_cancelled(self) -> None:
        import asyncio

        from app.services import automation
        playlist_id, _item_ids = self._playlist_and_items(1)
        with connect() as conn:
            task_id = to_int(conn.execute(
                """INSERT INTO recognition_tasks(playlist_id,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "queued", 1, utc_now(), utc_now()),
            ).lastrowid)
        gate = asyncio.Event()

        async def blocked(_item):
            await gate.wait()

        with patch.object(automation, "recognize_item", new=blocked):
            runner = asyncio.create_task(automation.run_recognition(task_id))
            await asyncio.sleep(0.05)
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner
        with connect() as conn:
            task = conn.execute("SELECT * FROM recognition_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "cancelled")

    async def test_run_library_scan_updates_states_and_counts(self) -> None:
        from app.services import library
        playlist_id, _item_ids = self._playlist_and_items(2)
        with connect() as conn:
            task_id = to_int(conn.execute(
                """INSERT INTO library_scan_tasks(playlist_id,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "queued", 2, utc_now(), utc_now()),
            ).lastrowid)
        states = [("in_library", "emby-1", "tag1"), ("strm", "emby-2", "tag2")]
        with patch.object(library, "library_details", new=AsyncMock(side_effect=states)):
            await library.run_library_scan(task_id)
        with connect() as conn:
            task = conn.execute("SELECT * FROM library_scan_tasks WHERE id=?", (task_id,)).fetchone()
            items = conn.execute(
                "SELECT library_state FROM playlist_items WHERE playlist_id=? ORDER BY rank_no", (playlist_id,),
            ).fetchall()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["in_library"], 1)
        self.assertEqual(task["strm"], 1)
        # as_completed 并发完成顺序不定，按集合断言。
        self.assertEqual({row["library_state"] for row in items}, {"in_library", "strm"})
        from app import tasks
        self.assertNotIn(task_id, tasks.LIBRARY.running)

    async def test_run_library_scan_marks_failed_on_error(self) -> None:
        from app.services import library
        playlist_id, _item_ids = self._playlist_and_items(1)
        with connect() as conn:
            task_id = to_int(conn.execute(
                """INSERT INTO library_scan_tasks(playlist_id,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "queued", 1, utc_now(), utc_now()),
            ).lastrowid)
        with patch.object(library, "library_details", new=AsyncMock(side_effect=RuntimeError("Emby 离线"))):
            await library.run_library_scan(task_id)
        with connect() as conn:
            task = conn.execute("SELECT * FROM library_scan_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "failed")
        self.assertIn("Emby 离线", task["error_message"] or "")

    async def test_run_library_scan_marks_partial_and_preserves_unknown(self) -> None:
        from app.services import library
        playlist_id, _item_ids = self._playlist_and_items(2)
        with connect() as conn:
            task_id = to_int(conn.execute(
                """INSERT INTO library_scan_tasks(playlist_id,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "queued", 2, utc_now(), utc_now()),
            ).lastrowid)

        lookup_calls = 0

        async def mixed_lookup(*_args: object, **_kwargs: object) -> tuple[str, str | None, str | None]:
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                return "unknown", None, None
            raise RuntimeError("Emby 请求失败 password=do-not-leak")

        with patch.object(library, "library_details", new=AsyncMock(side_effect=mixed_lookup)):
            await library.run_library_scan(task_id)
        with connect() as conn:
            task = conn.execute("SELECT * FROM library_scan_tasks WHERE id=?", (task_id,)).fetchone()
            items = conn.execute(
                "SELECT library_state FROM playlist_items WHERE playlist_id=? ORDER BY rank_no", (playlist_id,),
            ).fetchall()
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["completed"], 2)
        self.assertIn("Emby 请求失败", task["error_message"] or "")
        self.assertNotIn("do-not-leak", task["error_message"] or "")
        self.assertIn("unknown", {row["library_state"] for row in items})


class TmdbClientTests(IsolatedAppTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        settings.tr_base_url = ""
        settings.emby_base_url = ""
        settings.emby_api_key = ""

    async def test_tmdb_client_preserves_v3_prefix_when_normalizing_paths(self) -> None:
        previous_key = settings.tmdb_api_key
        previous_proxy = settings.outbound_proxy_url
        previous_tmdb_proxy = settings.tmdb_proxy_enabled
        settings.tmdb_api_key = "unit-test-tmdb-key"
        settings.outbound_proxy_url = ""
        settings.tmdb_proxy_enabled = False
        response = Mock()
        response.status_code = 200
        response.raise_for_status = Mock()
        try:
            with patch("app.clients.safe_request", new=AsyncMock(return_value=response)) as request:
                result = await TMDBClient().check()
            self.assertEqual(result, {"ok": True, "configured": True})
            call = request.await_args
            assert call is not None
            self.assertEqual(call.args[2], "configuration")
            self.assertEqual(str(call.args[0].base_url), "https://api.themoviedb.org/3/")
        finally:
            settings.tmdb_api_key = previous_key
            settings.outbound_proxy_url = previous_proxy
            settings.tmdb_proxy_enabled = previous_tmdb_proxy


class RecognitionLibraryTests(IsolatedAppTestCase):
    def _playlist(self, count: int = 2) -> int:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)", ("审计片单", 1, utc_now()),
            ).lastrowid)
            for rank in range(1, count + 1):
                conn.execute(
                    "INSERT INTO playlist_items(playlist_id,rank_no,original_title,year) VALUES(?,?,?,?)",
                    (playlist_id, rank, f"Movie {rank}", 2000 + rank),
                )
        return playlist_id

    def _recognition_task(self, playlist_id: int, total: int = 2) -> int:
        with connect() as conn:
            return to_int(conn.execute(
                "INSERT INTO recognition_tasks(playlist_id,status,total,created_at,updated_at) VALUES(?,?,?,?,?)",
                (playlist_id, "queued", total, utc_now(), utc_now()),
            ).lastrowid)

    def _task(self, table: str, task_id: int) -> sqlite3.Row:
        with connect() as conn:
            return conn.execute(f"SELECT * FROM {table} WHERE id=?", (task_id,)).fetchone()  # nosec B608

    async def test_failed_library_trigger_keeps_completed_recognition(self) -> None:
        from app.services import automation

        settings.tmdb_api_key = "tmdb-test"
        settings.emby_base_url, settings.emby_api_key = "http://emby.test", "emby-test"
        playlist_id = self._playlist()
        task_id = self._recognition_task(playlist_id)
        media = {"id": 1, "title": "Movie", "original_title": "Movie", "release_date": "2001-01-01"}
        with patch.object(automation, "recognize_item", new=AsyncMock(return_value=media)), \
             patch.object(automation, "persist_tmdb_item", new=Mock()), \
             patch.object(automation, "run_library_scan", new=Mock(side_effect=RuntimeError("scan boom"))):
            await automation.run_recognition(task_id)
        self.assertEqual(self._task("recognition_tasks", task_id)["status"], "completed")

    async def test_recognition_fails_fast_without_tmdb_key(self) -> None:
        from app.services import automation

        settings.tmdb_api_key = ""
        task_id = self._recognition_task(self._playlist())
        recognize = AsyncMock()
        with patch.object(automation, "recognize_item", new=recognize):
            await automation.run_recognition(task_id)
        task = self._task("recognition_tasks", task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("TMDB API Key", task["error_message"])
        recognize.assert_not_awaited()

    async def test_no_library_scan_without_emby(self) -> None:
        from app.api.playlists import scan_playlist_library
        from app.services import automation

        settings.emby_base_url, settings.emby_api_key = "", ""
        playlist_id = self._playlist()
        automation._trigger_post_recognition_library_scan(playlist_id)
        with connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM library_scan_tasks").fetchone()[0], 0)
        with self.assertRaises(HTTPException) as raised:
            await scan_playlist_library(playlist_id)
        self.assertEqual(raised.exception.status_code, 422)

    async def test_import_skips_auto_recognition_without_tmdb(self) -> None:
        from app.api.playlists import import_playlist

        settings.tmdb_api_key = ""
        result = await import_playlist(ImportPayload(json_data={"name": "导入", "films": [{"title": "Movie", "year": 2001}]}))
        self.assertIsNone(result["recognition_task_id"])
        self.assertIn("TMDB", result["recognition_note"])


class TmdbMatchTests(unittest.TestCase):
    def test_short_title_does_not_match_inside_other_words(self) -> None:
        from app.services.recognition import select_tmdb_match

        options = [
            {"id": 1, "title": "甲", "original_title": "Foo", "original_language": "en", "release_date": "2020-01-01"},
            {"id": 2, "title": "乙", "original_title": "B", "original_language": "en", "release_date": "2020-02-01"},
        ]
        self.assertIsNone(select_tmdb_match(options, "Obscure", 2020))

    def test_ambiguous_foreign_candidates_are_not_guessed(self) -> None:
        from app.services.recognition import select_tmdb_match

        options = [
            {"id": 1, "title": "甲", "original_title": "Jagten", "original_language": "da", "release_date": "2013-01-10"},
            {"id": 2, "title": "乙", "original_title": "Other", "original_language": "da", "release_date": "2012-03-01"},
        ]
        self.assertIsNone(select_tmdb_match(options, "The Hunt", 2012))
        exact_first = [
            {"id": 3, "title": "寄生虫", "original_title": "기생충", "original_language": "ko", "release_date": "2019-05-30"},
            {"id": 4, "title": "丙", "original_title": "Other", "original_language": "ko", "release_date": "2019-01-01"},
        ]
        self.assertEqual(select_tmdb_match(exact_first, "Parasite", 2019)["id"], 3)


class RecognitionClientTests(IsolatedAppTestCase):
    async def test_ai_preserves_configured_api_path(self):
        seen = []
        def handle(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"title":"Movie"}'}}]})
        client_type = httpx.AsyncClient
        def client_factory(**kwargs):
            return client_type(**kwargs, transport=httpx.MockTransport(handle))
        settings.ai_base_url = "https://example.org/v1"
        settings.ai_api_key = "audit-placeholder"
        settings.ai_model = "audit-model"
        with patch("app.clients.httpx.AsyncClient", side_effect=client_factory):
            result = await AIRecognitionClient().suggest("Movie", 2024)
        self.assertEqual(result, {"title": "Movie"})
        self.assertEqual(seen, ["/v1/chat/completions"])

    def test_select_tmdb_match_supports_unicode_and_no_blind_fallback(self):
        from app.services.recognition import select_tmdb_match

        # 1. 规范化支持中文字符正确匹配
        options = [
            {"id": 1, "title": "奥本海默", "original_title": "Oppenheimer", "release_date": "2023-07-21"},
            {"id": 2, "title": "芭比", "original_title": "Barbie", "release_date": "2023-07-21"},
        ]
        match = select_tmdb_match(options, "奥本海默", 2023)
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 1)

        # 2. 标题和年份完全不匹配时返回 None，绝不盲目返回第 0 项
        unrelated = [
            {"id": 99, "title": "热辣滚烫", "original_title": "YOLO", "release_date": "2024-02-10"},
        ]
        self.assertIsNone(select_tmdb_match(unrelated, "一部冷门未收录影片", 2020))


class RecognitionMatchTests(IsolatedAppTestCase):
    """以线上真实的误识别为样本：Cure (1997) 曾被配到 “Say It, Fight It, Cure It”。"""

    async def test_single_shared_word_is_not_a_title_match(self) -> None:
        from app.services.recognition import select_tmdb_match

        self.assertIsNone(select_tmdb_match(CURE_ZH_RESULTS, "Cure", 1997))

    async def test_main_title_and_word_coverage_still_match(self) -> None:
        from app.services.recognition import select_tmdb_match

        m = [{"id": 832, "title": "M就是凶手", "original_title": "M - Eine Stadt sucht einen Mörder",
              "original_language": "de", "release_date": "1931-05-11"}]
        self.assertEqual(select_tmdb_match(m, "M", 1931)["id"], 832)
        dr = [{"id": 935, "title": "奇爱博士", "original_title": "Dr. Strangelove or: How I Learned to Stop Worrying and Love the Bomb",
               "original_language": "en", "release_date": "1964-01-29"}]
        self.assertEqual(select_tmdb_match(dr, "Dr. Strangelove", 1964)["id"], 935)

    async def test_same_title_prefers_the_most_voted_film(self) -> None:
        from app.services.recognition import select_tmdb_match

        options = [
            {"id": 1, "title": "Psycho", "original_title": "Psycho", "release_date": "1960-06-01", "vote_count": 3},
            {"id": 539, "title": "惊魂记", "original_title": "Psycho", "release_date": "1960-06-22", "vote_count": 10000},
        ]
        self.assertEqual(select_tmdb_match(options, "Psycho", 1960)["id"], 539)

    async def test_english_search_recovers_international_title_and_keeps_localized_fields(self) -> None:
        from app.services import recognition

        settings.tmdb_language = "zh-CN"
        tmdb = AsyncMock()
        tmdb.search_movie.side_effect = [
            CURE_ZH_RESULTS,
            [{"id": 36095, "title": "Cure", "original_title": "キュア", "original_language": "ja",
              "release_date": "1997-12-27", "vote_count": 881}],
        ]
        tmdb.movie_details.return_value = {"title": "X圣治", "original_title": "キュア", "original_language": "ja",
                                           "release_date": "1997-12-27", "poster_path": "/cure.jpg"}
        tmdb.movie_external_ids.return_value = {"imdb_id": "tt0123948"}
        with patch.object(recognition, "TMDBClient", return_value=tmdb):
            media = await recognition.recognize_movie("Cure", 1997)
        self.assertEqual((media["id"], media["title"], media["imdb_id"]), (36095, "X圣治", "tt0123948"))
        self.assertEqual(tmdb.search_movie.await_args_list[1].kwargs, {"language": "en-US"})
        self.assertEqual(recognition.tmdb_original_language(media), "ja")

    async def test_imdb_id_is_trusted_even_when_titles_differ(self) -> None:
        from app.services import recognition

        tmdb = AsyncMock()
        tmdb.find_by_imdb.return_value = [{"id": 25538, "title": "一一", "original_title": "一一", "release_date": "2000-05-14"}]
        with patch.object(recognition, "TMDBClient", return_value=tmdb):
            media = await recognition.recognize_movie("Yi Yi", 2000, "tt0244316")
        self.assertEqual(media["id"], 25538)
        tmdb.search_movie.assert_not_awaited()
