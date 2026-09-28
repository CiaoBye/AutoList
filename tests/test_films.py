"""1.45 新界面“电影藏馆”：以影片为中心的状态计算与接口。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app import state
from app.api import films as film_routes
from app.api import system as system_routes
from app.config import settings
from app.database import SCHEMA_VERSION, connect
from app.security import is_signed_media_path
from app.services import films as film_service
from app.services.recognition import persist_tmdb_item
from app.util import to_int, utc_now
from tests.support import IsolatedAppTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FilmFixture(IsolatedAppTestCase):
    """A playlist whose items cover every film status and issue."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        now = utc_now()
        with connect() as conn:
            self.playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)", ("测试片单", 1, now),
            ).lastrowid)
            self.site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at) VALUES(?,?,?,?,?,?)",
                ("春天", "nexusphp", "https://site.example", 1, 1, now),
            ).lastrowid)
            self.items: dict[str, int] = {}
            specs = [
                ("in_library", 1, 101, "in_library"),
                ("missing", 2, 102, "not_found"),
                ("unchecked", 3, 103, "unknown"),
                ("unrecognized", 4, None, "not_found"),
                ("candidates", 5, 105, "not_found"),
                ("selected", 6, 106, "not_found"),
                ("downloading", 7, 107, "not_found"),
                ("no_eligible", 8, 108, "not_found"),
                ("submit_failed", 9, 109, "not_found"),
                ("strm", 10, 110, "strm"),
            ]
            for key, rank, tmdb_id, library_state in specs:
                self.items[key] = to_int(conn.execute(
                    """INSERT INTO playlist_items(
                         playlist_id,rank_no,original_title,year,tmdb_id,tmdb_title,tmdb_original_title,tmdb_year,
                         library_state,library_checked_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (self.playlist_id, rank, f"Movie {key}", 2000 + rank, tmdb_id,
                     f"电影{key}" if tmdb_id else None, f"Movie {key}" if tmdb_id else None,
                     2000 + rank if tmdb_id else None, library_state, now),
                ).lastrowid)
            self.task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,trigger,item_ids_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.playlist_id, 5, 8, "completed", 3, "manual",
                 json.dumps([self.items["candidates"], self.items["selected"], self.items["no_eligible"]]), now, now),
            ).lastrowid)
            for key in ("candidates", "selected", "no_eligible"):
                conn.execute(
                    """INSERT INTO search_attempts(task_id,playlist_item_id,site_id,site_name,status,result_count,duration_ms,created_at,finished_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (self.task_id, self.items[key], self.site_id, "春天", "success", 2, 500, now, now),
                )
            self.candidate_ids = {
                "candidates": self._candidate(conn, "candidates", "Movie.candidates.2005.1080p.BluRay.x265-CHD", "eligible"),
                "selected": self._candidate(conn, "selected", "Movie.selected.2006.1080p.BluRay.x265-CHD", "eligible"),
                "excluded": self._candidate(conn, "no_eligible", "Movie.no_eligible.2008.1080p.WEB-DL", "excluded", "WEB-DL 已排除"),
            }
            conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", (self.candidate_ids["selected"], now))
            conn.execute(
                """INSERT INTO download_history(playlist_item_id,title,torrent_name,site_name,submission_hash,success,message,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (self.items["downloading"], "电影downloading", "Movie.downloading.2007.1080p.x265-CHD", "春天", "hash7", 1, "ok", now),
            )
            conn.execute(
                """INSERT INTO download_history(playlist_item_id,title,torrent_name,site_name,success,message,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.items["submit_failed"], "电影submit_failed", "Movie.submit_failed.2009", "春天", 0,
                 "失败：https://site.example/download.php?passkey=SECRET_PASSKEY", now),
            )
        state.raw_candidates[self.candidate_ids["candidates"]] = {"media": {}, "torrent": {}}

    def _candidate(self, conn, key: str, title: str, eligibility: str, reason: str | None = None) -> str:
        candidate_id = f"cand-{key}-{eligibility}"
        conn.execute(
            """INSERT INTO candidates(
                 id,task_id,playlist_item_id,candidate_index,title,site_name,size,seeders,ranking,recommendation,
                 eligibility,exclusion_reason,resource_key,metadata_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (candidate_id, self.task_id, self.items[key], 0, title, "春天", 9_000_000_000, 12, 1,
             "preferred" if eligibility == "eligible" else "excluded", eligibility, reason, title, "{}", utc_now()),
        )
        return candidate_id

    def _no_transmission(self):
        return patch("app.services.films._current_downloads_cached", new=AsyncMock(return_value=([], "known_empty")))

    async def _films(self, **params):
        defaults = {"playlist_id": self.playlist_id, "status": "all", "q": "", "page": 1, "page_size": 60}
        with self._no_transmission():
            return await film_routes.films(**{**defaults, **params})


class FilmStatusTests(FilmFixture):
    async def test_every_status_is_resolved_in_priority_order(self) -> None:
        result = await self._films()
        by_id = {film["id"]: film for film in result["items"]}
        expected = {
            "in_library": "in_library", "missing": "missing", "unchecked": "unchecked",
            "unrecognized": "unrecognized", "candidates": "candidates", "selected": "selected",
            "downloading": "downloading", "no_eligible": "missing", "submit_failed": "missing", "strm": "missing",
        }
        for key, status in expected.items():
            with self.subTest(item=key):
                self.assertEqual(by_id[self.items[key]]["status"], status)
        self.assertEqual(by_id[self.items["no_eligible"]]["issues"], ["no_eligible"])
        self.assertEqual(by_id[self.items["submit_failed"]]["issues"], ["submit_failed"])
        # 购物车中的候选没有进程内下载上下文：提示需要重新寻片。
        self.assertEqual(by_id[self.items["selected"]]["issues"], ["context_expired"])
        self.assertEqual(by_id[self.items["candidates"]]["issues"], [])
        self.assertEqual(by_id[self.items["downloading"]]["transfer"], "waiting_library")
        self.assertEqual(result["counts"]["all"], 10)
        self.assertEqual(result["counts"]["missing"], 4)
        self.assertEqual(result["counts"]["issue:no_eligible"], 1)

    async def test_unknown_library_state_is_never_reported_as_missing(self) -> None:
        result = await self._films(status="unchecked")
        self.assertEqual([film["id"] for film in result["items"]], [self.items["unchecked"]])
        self.assertEqual(result["items"][0]["status_label"], "待核对")

    async def test_active_transmission_download_is_detected(self) -> None:
        torrents = [{"hashString": "hash7", "name": "Movie.downloading.2007.1080p.x265-CHD", "status": 4, "percentDone": 0.4}]
        with patch("app.services.films._current_downloads_cached", new=AsyncMock(return_value=(torrents, "known_present"))):
            result = await film_routes.films(playlist_id=self.playlist_id, status="downloading", q="", page=1, page_size=60)
        self.assertEqual(result["items"][0]["transfer"], "active")

    async def test_active_search_takes_precedence_over_missing(self) -> None:
        with connect() as conn:
            conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,item_ids_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (self.playlist_id, 2, 2, "running", 1, json.dumps([self.items["missing"]]), utc_now(), utc_now()),
            )
        result = await self._films(status="searching")
        self.assertEqual([film["id"] for film in result["items"]], [self.items["missing"]])

    async def test_filters_query_and_paging(self) -> None:
        issue = await self._films(status="issue:submit_failed")
        self.assertEqual([film["id"] for film in issue["items"]], [self.items["submit_failed"]])
        searched = await self._films(q="电影candidates")
        self.assertEqual([film["id"] for film in searched["items"]], [self.items["candidates"]])
        self.assertEqual(searched["counts"]["all"], 1)
        paged = await self._films(page=99, page_size=3)
        self.assertEqual((paged["page"], paged["pages"], len(paged["items"])), (4, 4, 1))
        with self.assertRaises(HTTPException) as invalid:
            await self._films(status="bogus")
        self.assertEqual(invalid.exception.status_code, 422)
        with self.assertRaises(HTTPException) as missing:
            await self._films(playlist_id=999)
        self.assertEqual(missing.exception.status_code, 404)

    async def test_film_detail_splits_candidates_and_sanitizes_history(self) -> None:
        with self._no_transmission():
            detail = await film_routes.film_detail(self.items["no_eligible"])
            failed = await film_routes.film_detail(self.items["submit_failed"])
            selected = await film_routes.film_detail(self.items["selected"])
        self.assertEqual(detail["candidates"], [])
        self.assertEqual(detail["excluded_summary"], [{"reason": "WEB-DL 已排除", "count": 1}])
        self.assertEqual(detail["search"]["sites"], 1)
        self.assertNotIn("SECRET_PASSKEY", json.dumps(failed["history"], ensure_ascii=False))
        self.assertEqual(selected["candidates"][0]["in_cart"], 1)
        self.assertFalse(selected["candidates"][0]["context_available"])
        with self.assertRaises(HTTPException) as missing:
            await film_routes.film_detail(99999)
        self.assertEqual(missing.exception.status_code, 404)

    async def test_home_summarises_todos_and_recent(self) -> None:
        with self._no_transmission(), patch("app.api.films.hydrate_recent_emby_posters", new=AsyncMock()):
            home = await film_routes.home(playlist_id=None)
        todos = {todo["key"]: todo["count"] for todo in home["todos"]}
        self.assertEqual(home["playlist_id"], self.playlist_id)
        # 缺片 4 部中有 1 部无合格资源，不再重复提示寻片。
        self.assertEqual(todos["search_missing"], 3)
        self.assertEqual(todos["pick"], 1)
        self.assertEqual(todos["submit"], 1)
        self.assertEqual(todos["unrecognized"], 1)
        self.assertEqual(todos["unchecked"], 1)
        self.assertEqual([film["id"] for film in home["recent"]], [self.items["in_library"]])
        # “接下来寻片”与寻片按钮同一范围：可寻片的缺片，按片单顺序。
        self.assertEqual(len(home["up_next"]), todos["search_missing"])
        self.assertTrue(all(film["status"] == "missing" and "no_eligible" not in film["issues"] for film in home["up_next"]))
        ranks = [film["rank_no"] for film in home["up_next"]]
        self.assertEqual(ranks, sorted(ranks))


class FilmSearchTests(FilmFixture):
    async def test_single_film_search_creates_snapshot_task(self) -> None:
        with patch("app.api.films.run_search", new=AsyncMock()):
            created = await film_routes.search_film(self.items["missing"])
        with connect() as conn:
            task = conn.execute("SELECT * FROM search_tasks WHERE id=?", (created["id"],)).fetchone()
        self.assertEqual(json.loads(task["item_ids_json"]), [self.items["missing"]])
        self.assertEqual((task["trigger"], task["total"], task["status"]), ("film", 1, "queued"))
        self.assertEqual(json.loads(task["site_ids_json"]), [self.site_id])
        with self.assertRaises(HTTPException) as duplicate:
            await film_routes.search_film(self.items["missing"])
        self.assertEqual(duplicate.exception.status_code, 409)

    async def test_single_film_search_rejects_library_items_and_missing_sites(self) -> None:
        with self.assertRaises(HTTPException) as in_library:
            await film_routes.search_film(self.items["in_library"])
        self.assertEqual(in_library.exception.status_code, 409)
        with connect() as conn:
            conn.execute("UPDATE pt_sites SET search_enabled=0")
        with self.assertRaises(HTTPException) as no_sites:
            await film_routes.search_film(self.items["missing"])
        self.assertEqual(no_sites.exception.status_code, 422)


class TmdbPosterTests(FilmFixture):
    async def test_poster_url_requires_tmdb_configuration(self) -> None:
        settings.tmdb_api_key = ""
        result = await self._films(status="missing")
        self.assertTrue(all(film["poster_url"] is None for film in result["items"]))
        settings.tmdb_api_key = "tmdb-test"
        result = await self._films(status="missing")
        self.assertTrue(all(film["poster_url"].startswith("/api/playlist-items/") for film in result["items"]))
        self.assertTrue(is_signed_media_path(f"/api/playlist-items/{self.items['missing']}/tmdb-poster"))

    async def test_poster_path_is_backfilled_validated_and_cached(self) -> None:
        settings.tmdb_api_key = "tmdb-test"
        item_id = self.items["missing"]
        details = AsyncMock(return_value={"poster_path": "/abc123.jpg"})
        image = AsyncMock(return_value=(PNG, "image/png"))
        with patch("app.api.films.TMDBClient.movie_details", new=details), \
             patch("app.api.films.TMDBClient.poster_image", new=image):
            first = await film_routes.playlist_item_tmdb_poster(item_id)
            second = await film_routes.playlist_item_tmdb_poster(item_id)
        self.assertEqual((first.status_code, first.media_type), (200, "image/png"))
        self.assertEqual(second.body, PNG)
        details.assert_awaited_once()
        image.assert_awaited_once_with("/abc123.jpg")
        with connect() as conn:
            stored = conn.execute("SELECT tmdb_poster_path FROM playlist_items WHERE id=?", (item_id,)).fetchone()[0]
        self.assertEqual(stored, "/abc123.jpg")

    async def test_unsafe_or_missing_poster_paths_are_rejected(self) -> None:
        settings.tmdb_api_key = "tmdb-test"
        item_id = self.items["candidates"]
        with patch("app.api.films.TMDBClient.movie_details", new=AsyncMock(return_value={"poster_path": "https://evil.example/x.jpg"})):
            with self.assertRaises(HTTPException) as rejected:
                await film_routes.playlist_item_tmdb_poster(item_id)
        self.assertEqual(rejected.exception.status_code, 404)
        with connect() as conn:
            stored = conn.execute("SELECT tmdb_poster_path FROM playlist_items WHERE id=?", (item_id,)).fetchone()[0]
        # 记住“没有海报”，之后不再重复请求 TMDB，列表也不再生成海报地址。
        self.assertEqual(stored, "")
        result = await self._films(status="candidates")
        self.assertIsNone(result["items"][0]["poster_url"])
        with self.assertRaises(HTTPException) as unrecognized:
            await film_routes.playlist_item_tmdb_poster(self.items["unrecognized"])
        self.assertEqual(unrecognized.exception.status_code, 404)

    async def test_poster_endpoint_without_tmdb_key_is_not_found(self) -> None:
        settings.tmdb_api_key = ""
        with self.assertRaises(HTTPException) as raised:
            await film_routes.playlist_item_tmdb_poster(self.items["missing"])
        self.assertEqual(raised.exception.status_code, 404)

    async def test_recognition_persists_only_tmdb_shaped_paths(self) -> None:
        item_id = self.items["missing"]
        persist_tmdb_item(item_id, {"id": 5, "title": "T", "original_title": "T", "release_date": "2001-01-01", "poster_path": "/ok_1.png"})
        persist_tmdb_item(item_id, {"id": 5, "title": "T", "original_title": "T", "release_date": "2001-01-01", "poster_path": "../x"})
        with connect() as conn:
            stored = conn.execute("SELECT tmdb_poster_path FROM playlist_items WHERE id=?", (item_id,)).fetchone()[0]
        self.assertEqual(stored, "/ok_1.png")


# 当前 fanart.tv 返回的地址格式；早期的 /fanart/movies/<编号>/movieposter/ 格式同样接受。
FANART_URL = "https://assets.fanart.tv/fanart/movie-5213f9ee6ab1b.jpg"
LEGACY_FANART_URL = "https://assets.fanart.tv/fanart/movies/102/movieposter/movie-legacy.jpg"


class FanartPosterTests(FilmFixture):
    def _fanart_row(self, item_id: int) -> str | None:
        with connect() as conn:
            return conn.execute("SELECT fanart_poster_url FROM playlist_items WHERE id=?", (item_id,)).fetchone()[0]

    async def test_poster_url_prefers_fanart_only_when_configured(self) -> None:
        settings.tmdb_api_key = "tmdb-test"
        settings.fanart_api_key = ""
        result = await self._films(status="missing")
        self.assertTrue(all("/tmdb-poster" in film["poster_url"] for film in result["items"]))
        settings.fanart_api_key = "fanart-test"
        result = await self._films(status="missing")
        self.assertTrue(all("/fanart-poster?v=pending" in film["poster_url"] for film in result["items"]))
        self.assertTrue(is_signed_media_path(f"/api/playlist-items/{self.items['missing']}/fanart-poster"))
        # 已确认 fanart.tv 没有海报的影片回到 TMDB 海报；未识别的影片没有海报。
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET fanart_poster_url='' WHERE id=?", (self.items["missing"],))
        detail = await film_routes.film_detail(self.items["missing"])
        self.assertIn("/tmdb-poster", detail["poster_url"])
        unrecognized = await film_routes.film_detail(self.items["unrecognized"])
        self.assertIsNone(unrecognized["poster_url"])

    async def test_best_poster_is_chosen_stored_and_cached(self) -> None:
        settings.fanart_api_key = "fanart-test"
        settings.tmdb_language = "zh-CN"
        item_id = self.items["missing"]
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET tmdb_original_language='ja' WHERE id=?", (item_id,))
        payload = {"movieposter": [
            {"url": "https://evil.example/x.jpg", "lang": "ja", "likes": "99"},
            {"url": FANART_URL.replace("movie-", "zh-"), "lang": "zh", "likes": "80"},
            {"url": FANART_URL.replace("movie-", "en-"), "lang": "en", "likes": "50"},
            {"url": FANART_URL.replace("movie-", "textless-"), "lang": "00", "likes": "40"},
            {"url": FANART_URL.replace("https://", "http://"), "lang": "ja", "likes": "3"},
            {"url": FANART_URL.replace("movie-", "ja-low-"), "lang": "ja", "likes": "1"},
            {"url": LEGACY_FANART_URL, "lang": "ja", "likes": "2"},
        ]}
        lookup = AsyncMock(return_value=httpx.Response(200, json=payload, request=httpx.Request("GET", "https://webservice.fanart.tv/v3/movies/102")))
        image = AsyncMock(return_value=(PNG, "image/png"))
        with patch("app.clients.FanartClient._movie", new=lookup), \
             patch("app.api.films.FanartClient.poster_image", new=image):
            first = await film_routes.playlist_item_fanart_poster(item_id)
            second = await film_routes.playlist_item_fanart_poster(item_id)
        self.assertEqual((first.status_code, first.media_type, second.body), (200, "image/png", PNG))
        # 未带版本号（待定地址）只短期缓存；带上当前海报的版本号才长期缓存。
        self.assertEqual(first.headers["Cache-Control"], film_routes.FALLBACK_POSTER_CACHE)
        versioned = await film_routes.playlist_item_fanart_poster(item_id, film_service.fanart_version(FANART_URL))
        self.assertIn("max-age=604800", versioned.headers["Cache-Control"])
        lookup.assert_awaited_once_with(102)
        # 非 fanart.tv 地址被丢弃；日语片取日文版（不看 TMDB 显示语言），同语言取点赞多的，http 统一为 https。
        image.assert_awaited_once_with(FANART_URL)
        self.assertEqual(self._fanart_row(item_id), FANART_URL)
        result = await self._films(status="missing")
        film = next(film for film in result["items"] if film["id"] == item_id)
        self.assertIn(f"v={film_service.fanart_version(FANART_URL)}", film["poster_url"])

    async def test_poster_language_order_follows_original_language(self) -> None:
        from app.clients import fanart_poster_rank

        posters = [{"lang": lang, "likes": "1"} for lang in ("fr", "en", "00", "ja")]
        order = lambda language: [p["lang"] for p in sorted(posters, key=lambda p: fanart_poster_rank(p, language))]
        self.assertEqual(order("ja"), ["ja", "00", "en", "fr"])
        self.assertEqual(order("en"), ["en", "00", "fr", "ja"])
        # 原语言未知时：无字版、英文版优先。
        self.assertEqual(order(None), ["00", "en", "fr", "ja"])

    async def test_original_language_is_backfilled_from_tmdb_once(self) -> None:
        settings.fanart_api_key = "fanart-test"
        settings.tmdb_api_key = "tmdb-test"
        item_id = self.items["missing"]
        details = AsyncMock(return_value={"original_language": "cn"})
        posters = AsyncMock(return_value="")
        tmdb = AsyncMock(return_value=film_routes.Response(content=PNG, media_type="image/png"))
        with patch("app.api.films.TMDBClient.movie_details", new=details), \
             patch("app.api.films.FanartClient.movie_poster_url", new=posters), \
             patch("app.api.films.playlist_item_tmdb_poster", new=tmdb):
            await film_routes.playlist_item_fanart_poster(item_id)
        # 粤语片（TMDB 记为 cn）按中文海报挑选。
        posters.assert_awaited_once_with(102, "zh")
        with connect() as conn:
            stored = conn.execute("SELECT tmdb_original_language FROM playlist_items WHERE id=?", (item_id,)).fetchone()[0]
        self.assertEqual(stored, "zh")

    async def test_missing_fanart_poster_falls_back_and_is_remembered(self) -> None:
        settings.fanart_api_key = "fanart-test"
        settings.tmdb_api_key = "tmdb-test"
        item_id = self.items["missing"]
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET tmdb_original_language='en' WHERE id=?", (item_id,))
        tmdb = AsyncMock(return_value=film_routes.Response(content=PNG, media_type="image/png"))
        with patch("app.clients.FanartClient._movie", new=AsyncMock(return_value=httpx.Response(404, json={}))), \
             patch("app.api.films.playlist_item_tmdb_poster", new=tmdb):
            response = await film_routes.playlist_item_fanart_poster(item_id)
        self.assertEqual(response.body, PNG)
        self.assertEqual(response.headers["Cache-Control"], film_routes.FALLBACK_POSTER_CACHE)
        self.assertEqual(self._fanart_row(item_id), "")

    async def test_lookup_failure_falls_back_without_remembering(self) -> None:
        settings.fanart_api_key = "fanart-test"
        settings.tmdb_api_key = "tmdb-test"
        item_id = self.items["missing"]
        tmdb = AsyncMock(return_value=film_routes.Response(content=PNG, media_type="image/png"))
        with patch("app.clients.FanartClient._movie", new=AsyncMock(side_effect=httpx.ConnectError("down"))), \
             patch("app.api.films.playlist_item_tmdb_poster", new=tmdb):
            response = await film_routes.playlist_item_fanart_poster(item_id)
        self.assertEqual(response.body, PNG)
        self.assertIsNone(self._fanart_row(item_id))
        with self.assertRaises(HTTPException) as unrecognized:
            await film_routes.playlist_item_fanart_poster(self.items["unrecognized"])
        self.assertEqual(unrecognized.exception.status_code, 404)

    async def test_both_fanart_url_formats_are_accepted(self) -> None:
        from app.clients import FANART_POSTER_URL

        self.assertTrue(FANART_POSTER_URL.fullmatch(FANART_URL))
        self.assertTrue(FANART_POSTER_URL.fullmatch(LEGACY_FANART_URL))
        self.assertFalse(FANART_POSTER_URL.fullmatch("https://assets.fanart.tv/fanart/../x.jpg/evil"))
        self.assertFalse(FANART_POSTER_URL.fullmatch("https://evil.example/fanart/movie.jpg"))

    async def test_upgrade_rechecks_films_wrongly_marked_without_poster_once(self) -> None:
        from app.database import initialize

        with connect() as conn:
            conn.execute("UPDATE playlist_items SET fanart_poster_url='' WHERE id=?", (self.items["missing"],))
            conn.execute("PRAGMA user_version=6")
        initialize()
        self.assertIsNone(self._fanart_row(self.items["missing"]))
        # 之后确认没有海报的记录不再被清掉。
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET fanart_poster_url='' WHERE id=?", (self.items["missing"],))
        initialize()
        self.assertEqual(self._fanart_row(self.items["missing"]), "")

    async def test_reidentification_resets_fanart_poster(self) -> None:
        item_id = self.items["missing"]
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET fanart_poster_url=? WHERE id=?", (FANART_URL, item_id))
        media = {"id": 102, "title": "T", "original_title": "T", "release_date": "2001-01-01"}
        persist_tmdb_item(item_id, media)
        self.assertEqual(self._fanart_row(item_id), FANART_URL)
        persist_tmdb_item(item_id, {**media, "id": 999})
        self.assertIsNone(self._fanart_row(item_id))
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET fanart_poster_url=? WHERE id=?", (FANART_URL, item_id))
        film_service.reidentify_item(item_id, {**media, "id": 1000})
        self.assertIsNone(self._fanart_row(item_id))

    async def test_settings_keep_fanart_key_server_side(self) -> None:
        from app.main import app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            saved = await client.put("/api/settings", json={"fanart_api_key": "fanart-secret-value"})
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["fanart_api_key"], "")
            self.assertTrue(saved.json()["fanart_api_key_configured"])
            self.assertNotIn("fanart-secret-value", saved.text)
            kept = await client.put("/api/settings", json={"fanart_api_key": None})
            self.assertTrue(kept.json()["fanart_api_key_configured"])
            cleared = await client.put("/api/settings", json={"clear_fanart_api_key": True})
            self.assertFalse(cleared.json()["fanart_api_key_configured"])


class NextEntryTests(IsolatedAppTestCase):
    async def test_schema_has_poster_column(self) -> None:
        with connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlist_items)")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("tmdb_poster_path", columns)
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 5)

    async def test_root_reports_missing_build_and_serves_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.html"
            with patch.object(system_routes, "UI_INDEX", index):
                missing = await system_routes.index()
                index.write_text("<!doctype html><title>ui</title>", encoding="utf-8")
                served = await system_routes.index()
        self.assertEqual(missing.status_code, 503)
        self.assertIn("npm", missing.body.decode("utf-8"))
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["cache-control"], "no-cache, must-revalidate")

    async def test_entry_is_public_but_film_api_requires_token(self) -> None:
        import httpx

        from app.main import app

        os.environ["AUTOLIST_ACCESS_TOKEN"] = "x" * 20 + "abcdefghijklmnop"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            entry = await client.get("/")
            films = await client.get("/api/films")
            poster = await client.get("/api/playlist-items/1/tmdb-poster")
        self.assertNotIn(entry.status_code, {401, 403})
        self.assertEqual(films.status_code, 401)
        self.assertEqual(poster.status_code, 401)

    async def test_film_status_labels_cover_every_status(self) -> None:
        self.assertEqual(set(film_service.FILM_STATUS_LABELS), set(film_service.FILM_STATUSES))


class CandidateTaskChainTests(FilmFixture):
    def _followup(self, conn, parent_id: int, trigger: str, candidate_id: str, site: str) -> int:
        now = utc_now()
        task_id = to_int(conn.execute(
            """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,parent_task_id,trigger,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (self.playlist_id, 5, 5, "completed", 1, parent_id, trigger, now, now),
        ).lastrowid)
        conn.execute(
            """INSERT INTO candidates(
                 id,task_id,playlist_item_id,candidate_index,title,site_name,size,seeders,ranking,recommendation,
                 eligibility,resource_key,metadata_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (candidate_id, task_id, self.items["candidates"], 0, f"Other.{candidate_id}", site, 8_000_000_000, 5, 2,
             "preferred", "eligible", candidate_id, "{}", now),
        )
        return task_id

    async def test_retry_keeps_parent_candidates_but_restart_replaces_them(self) -> None:
        item_id = self.items["candidates"]
        with connect() as conn:
            retry_id = self._followup(conn, self.task_id, "retry", "cand-retry", "夏天")
        # “重试失败的站点”只补搜失败组合：原任务的候选仍然有效，与重试结果合并展示。
        with self._no_transmission():
            detail = await film_routes.film_detail(item_id)
        self.assertEqual({c["id"] for c in detail["candidates"]}, {self.candidate_ids["candidates"], "cand-retry"})
        self.assertEqual(detail["status"], "candidates")
        with connect() as conn:
            self._followup(conn, retry_id, "restart", "cand-restart", "秋天")
        # “重新开始”是完整范围重搜，只看它自己的结果。
        with self._no_transmission():
            detail = await film_routes.film_detail(item_id)
        self.assertEqual([c["id"] for c in detail["candidates"]], ["cand-restart"])


class PickQueueTests(FilmFixture):
    async def test_pick_queue_groups_selectable_and_blocked_films(self) -> None:
        with self._no_transmission():
            queue = await film_routes.picks(playlist_id=self.playlist_id, status="all")
        buckets = {item["id"]: item["bucket"] for item in queue["items"]}
        self.assertEqual(queue["counts"], {"all": 3, "candidates": 1, "selected": 1, "no_eligible": 1})
        self.assertEqual(buckets[self.items["candidates"]], "candidates")
        self.assertEqual(buckets[self.items["selected"]], "selected")
        self.assertEqual(buckets[self.items["no_eligible"]], "no_eligible")
        by_id = {item["id"]: item for item in queue["items"]}
        self.assertTrue(by_id[self.items["candidates"]]["candidates"][0]["context_available"])
        self.assertEqual(by_id[self.items["selected"]]["candidates"][0]["in_cart"], 1)
        self.assertEqual(by_id[self.items["no_eligible"]]["candidates"], [])
        self.assertEqual(by_id[self.items["no_eligible"]]["excluded_count"], 1)
        with self._no_transmission():
            selected = await film_routes.picks(playlist_id=self.playlist_id, status="selected")
        self.assertEqual([item["id"] for item in selected["items"]], [self.items["selected"]])
        with self.assertRaises(HTTPException) as invalid:
            await film_routes.picks(playlist_id=None, status="bogus")
        self.assertEqual(invalid.exception.status_code, 422)


class TimelineTests(FilmFixture):
    async def test_timeline_merges_sources_and_filters(self) -> None:
        with patch("app.api.films.log_events", new=AsyncMock(return_value=[
            {"ts": "2099-01-01T00:00:00+00:00", "level": "INFO", "event": "playlist_imported", "detail": "导入片单【测试】"},
            {"ts": "2099-01-01T00:00:01+00:00", "level": "INFO", "event": "cookiecloud_sync", "detail": "不应出现"},
        ])):
            everything = await film_routes.timeline(type="all", film_id=None, limit=120)
            films = await film_routes.timeline(type="films", film_id=None, limit=120)
            system = await film_routes.timeline(type="system", film_id=None, limit=120)
            one = await film_routes.timeline(type="all", film_id=self.items["selected"], limit=120)
        kinds = {event["kind"] for event in everything["items"]}
        self.assertTrue({"submit", "search", "system"} <= kinds)
        # 日志只收录白名单事件，并按时间倒序。
        self.assertEqual(everything["items"][0]["title"], "导入片单")
        self.assertNotIn("不应出现", json.dumps(everything, ensure_ascii=False))
        self.assertNotIn("SECRET_PASSKEY", json.dumps(everything, ensure_ascii=False))
        self.assertTrue(all(event["film_id"] is not None for event in films["items"]))
        self.assertTrue(all(event["film_id"] is None for event in system["items"]))
        # 批量搜索任务包含该影片，按影片筛选时也要出现。
        self.assertEqual([event["kind"] for event in one["items"]], ["search"])
        with self.assertRaises(HTTPException) as invalid:
            await film_routes.timeline(type="bogus", film_id=None, limit=10)
        self.assertEqual(invalid.exception.status_code, 422)


class IdentityCorrectionTests(FilmFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        settings.tmdb_api_key = "tmdb-test"
        with connect() as conn:
            conn.execute(
                """UPDATE playlist_items SET tmdb_poster_path='/wrong.jpg', emby_item_id='99', emby_image_tag='tag'
                   WHERE id=?""",
                (self.items["missing"],),
            )

    def _row(self) -> dict:
        with connect() as conn:
            return dict(conn.execute("SELECT * FROM playlist_items WHERE id=?", (self.items["missing"],)).fetchone())

    async def test_recognize_replaces_identity_and_rechecks_emby(self) -> None:
        settings.emby_base_url, settings.emby_api_key = "http://emby.test", "emby-test"
        media = {"id": 4242, "title": "X圣治", "original_title": "CURE", "release_date": "1997-12-27",
                 "imdb_id": "tt0123948", "poster_path": "/cure.jpg"}
        with patch("app.api.films.recognize_item", new=AsyncMock(return_value=media)) as recognize, \
             patch("app.api.films.library_details", new=AsyncMock(return_value=("in_library", "501", "tag501"))), \
             self._no_transmission():
            detail = await film_routes.recognize_film(self.items["missing"])
        recognize.assert_awaited_once()
        self.assertEqual(recognize.await_args.args[0]["original_title"], "Movie missing")
        row = self._row()
        self.assertEqual((row["tmdb_id"], row["tmdb_title"], row["tmdb_poster_path"]), (4242, "X圣治", "/cure.jpg"))
        self.assertEqual((row["library_state"], row["emby_item_id"]), ("in_library", "501"))
        self.assertEqual(detail["status"], "in_library")

    async def test_recognize_without_match_keeps_existing_identity(self) -> None:
        with patch("app.api.films.recognize_item", new=AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as raised:
                await film_routes.recognize_film(self.items["missing"])
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(self._row()["tmdb_id"], 102)

    async def test_manual_tmdb_assignment_clears_stale_media_links(self) -> None:
        settings.emby_base_url, settings.emby_api_key = "", ""
        details = {"id": 777, "title": "一一", "original_title": "一一", "release_date": "2000-05-14", "imdb_id": "tt0244316"}
        with patch("app.api.films.TMDBClient.movie_details", new=AsyncMock(return_value=details)), self._no_transmission():
            detail = await film_routes.set_film_tmdb(self.items["missing"], film_routes.FilmTmdbPayload(tmdb_id=777))
        row = self._row()
        self.assertEqual((row["tmdb_id"], row["tmdb_imdb_id"]), (777, "tt0244316"))
        # 没有海报路径时清空待补取；旧的 Emby 关联作废，未配置 Emby 时显示“待核对”。
        self.assertIsNone(row["tmdb_poster_path"])
        self.assertIsNone(row["emby_item_id"])
        self.assertEqual(detail["status"], "unchecked")

    async def test_manual_assignment_rejects_unknown_or_inconsistent_ids(self) -> None:
        import httpx

        request = httpx.Request("GET", "https://api.themoviedb.org/3/movie/5")
        not_found = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        with patch("app.api.films.TMDBClient.movie_details", new=AsyncMock(side_effect=not_found)):
            with self.assertRaises(HTTPException) as missing:
                await film_routes.set_film_tmdb(self.items["missing"], film_routes.FilmTmdbPayload(tmdb_id=5))
        self.assertEqual(missing.exception.status_code, 404)
        with patch("app.api.films.TMDBClient.movie_details", new=AsyncMock(return_value={"id": 6})):
            with self.assertRaises(HTTPException) as mismatch:
                await film_routes.set_film_tmdb(self.items["missing"], film_routes.FilmTmdbPayload(tmdb_id=5))
        self.assertEqual(mismatch.exception.status_code, 502)
        self.assertEqual(self._row()["tmdb_id"], 102)

    async def test_tmdb_matches_mark_current_identity(self) -> None:
        options = [
            {"id": 102, "title": "当前", "original_title": "Current", "release_date": "2002-01-01", "overview": "x" * 300},
            {"id": 9, "title": "别的", "original_title": "Other", "release_date": ""},
        ]
        with patch("app.api.films.TMDBClient.search_movie", new=AsyncMock(return_value=options)) as search:
            matches = await film_routes.tmdb_matches(self.items["missing"], q="", year=None)
        search.assert_awaited_once_with("Movie missing", 2002)
        self.assertEqual([(item["tmdb_id"], item["current"]) for item in matches], [(102, True), (9, False)])
        self.assertEqual(len(matches[0]["overview"]), 140)
        self.assertIsNone(matches[1]["year"])
        settings.tmdb_api_key = ""
        with self.assertRaises(HTTPException) as no_key:
            await film_routes.tmdb_matches(self.items["missing"], q="x", year=None)
        self.assertEqual(no_key.exception.status_code, 422)


class SettingsKeepTests(IsolatedAppTestCase):
    async def test_null_cookiecloud_url_keeps_saved_address(self) -> None:
        import httpx

        from app.main import app

        settings.cookiecloud_url = "http://cc.example:8088/cookiecloud"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            kept = await client.put("/api/settings", json={"cookiecloud_url": None, "tmdb_proxy_enabled": True})
            self.assertEqual(kept.status_code, 200, kept.text)
            self.assertEqual(settings.cookiecloud_url, "http://cc.example:8088/cookiecloud")
            cleared = await client.put("/api/settings", json={"clear_cookiecloud_url": True})
            self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(settings.cookiecloud_url, "")


CURE_ZH_RESULTS = [
    {"id": 6715, "title": "鳄鱼波鞋走天涯", "original_title": "The Cure", "original_language": "en",
     "release_date": "1995-04-21", "vote_count": 368},
    {"id": 1199410, "title": "Say It, Fight It, Cure It", "original_title": "Say It, Fight It, Cure It",
     "original_language": "en", "release_date": "1997-10-05", "vote_count": 1},
    {"id": 36095, "title": "X圣治", "original_title": "キュア", "original_language": "ja",
     "release_date": "1997-12-27", "vote_count": 881},
]


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


class SourceIdentityTests(FilmFixture):
    """来源自带身份优先：Letterboxd 影片页的 TMDB 编号 → IMDb → 片名搜索兜底。"""

    async def test_source_tmdb_id_is_used_directly_without_title_search(self) -> None:
        from app.services import recognition

        tmdb = AsyncMock()
        tmdb.movie_details.return_value = {"id": 36095, "title": "X圣治", "original_title": "キュア",
                                           "release_date": "1997-12-27", "imdb_id": "tt0123948", "original_language": "ja"}
        with patch.object(recognition, "TMDBClient", return_value=tmdb):
            media = await recognition.recognize_movie("Cure", 1997, None, 36095)
        self.assertEqual((media["id"], media["imdb_id"]), (36095, "tt0123948"))
        tmdb.search_movie.assert_not_awaited()
        tmdb.find_by_imdb.assert_not_awaited()

    async def test_deleted_source_tmdb_id_falls_back_to_search(self) -> None:
        from app.services import recognition

        tmdb = AsyncMock()
        request = httpx.Request("GET", "https://api.themoviedb.org/3/movie/1")
        tmdb.movie_details.side_effect = httpx.HTTPStatusError("gone", request=request, response=httpx.Response(404, request=request))
        tmdb.search_movie.return_value = [{"id": 7, "title": "Cure", "original_title": "Cure", "release_date": "1997-01-01"}]
        tmdb.movie_external_ids.return_value = {}
        with patch.object(recognition, "TMDBClient", return_value=tmdb):
            media = await recognition.recognize_movie("Cure", 1997, None, 1)
        self.assertEqual(media["id"], 7)

    async def test_letterboxd_ids_are_fetched_once_and_stored(self) -> None:
        from app.services import recognition

        item_id = self.items["missing"]
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET source_ref='letterboxd:cure',imdb_id=NULL WHERE id=?", (item_id,))
            row = dict(conn.execute("SELECT * FROM playlist_items WHERE id=?", (item_id,)).fetchone())
        lookup = AsyncMock(return_value=(36095, "tt0123948"))
        recognize = AsyncMock(return_value={"id": 36095})
        with patch("app.list_sources.PlaylistSourceFetcher.letterboxd_film_ids", new=lookup), \
             patch.object(recognition, "recognize_movie", new=recognize):
            await recognition.recognize_item(row)
        lookup.assert_awaited_once_with("cure")
        recognize.assert_awaited_once_with("Movie missing", 2002, "tt0123948", 36095)
        with connect() as conn:
            stored = conn.execute("SELECT source_tmdb_id,imdb_id FROM playlist_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual(tuple(stored), (36095, "tt0123948"))

    async def test_letterboxd_outage_falls_back_to_title_recognition(self) -> None:
        from app.services import recognition

        row = {"id": self.items["missing"], "original_title": "Cure", "year": 1997, "imdb_id": None,
               "source_tmdb_id": None, "source_ref": "letterboxd:cure"}
        recognize = AsyncMock(return_value=None)
        with patch("app.list_sources.PlaylistSourceFetcher.letterboxd_film_ids", new=AsyncMock(side_effect=httpx.ConnectError("down"))), \
             patch.object(recognition, "recognize_movie", new=recognize):
            await recognition.recognize_item(row)
        recognize.assert_awaited_once_with("Cure", 1997, None, None)

    async def test_letterboxd_film_page_parsing(self) -> None:
        from app.list_sources import PlaylistSourceFetcher

        html = ('<body data-tmdb-type="movie" data-tmdb-id="36095">'
                '<a href="http://www.imdb.com/title/tt0123948/maindetails">IMDb</a></body>')
        tv = '<body data-tmdb-type="tv" data-tmdb-id="1399"></body>'
        request = httpx.Request("GET", "https://embed.letterboxd.com/film/cure/")
        for body, expected in ((html, (36095, "tt0123948")), (tv, (None, None))):
            with patch("app.list_sources.safe_request", new=AsyncMock(return_value=httpx.Response(200, text=body, request=request))):
                self.assertEqual(await PlaylistSourceFetcher().letterboxd_film_ids("cure"), expected)
        with self.assertRaises(ValueError):
            await PlaylistSourceFetcher().letterboxd_film_ids("../etc")

    async def test_incremental_sync_backfills_source_identity_without_touching_items(self) -> None:
        from app.services import automation

        with connect() as conn:
            conn.execute("UPDATE playlists SET source_url='https://letterboxd.com/test/list/sample/' WHERE id=?", (self.playlist_id,))
            before = dict(conn.execute("SELECT * FROM playlist_items WHERE id=?", (self.items["missing"],)).fetchone())

        async def fetch_source(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {"source_name": "来源", "items": [
                {"rank_no": 1, "original_title": "Movie missing", "year": 2002, "source_ref": "letterboxd:movie-missing"},
            ]}

        with patch("app.services.automation.PlaylistSourceFetcher.fetch", new=fetch_source):
            await automation.sync_playlist_incremental(self.playlist_id)
        with connect() as conn:
            after = dict(conn.execute("SELECT * FROM playlist_items WHERE id=?", (self.items["missing"],)).fetchone())
        self.assertEqual(after["source_ref"], "letterboxd:movie-missing")
        self.assertEqual({k: after[k] for k in ("rank_no", "tmdb_id", "tmdb_title")}, {k: before[k] for k in ("rank_no", "tmdb_id", "tmdb_title")})


class BackdropTests(FilmFixture):
    """影片详情横幅：fanart.tv 无字剧照优先，没有时退回 TMDB 剧照。"""

    def _row(self, item_id: int) -> tuple:
        with connect() as conn:
            return tuple(conn.execute(
                "SELECT fanart_backdrop_url,tmdb_backdrop_path FROM playlist_items WHERE id=?", (item_id,),
            ).fetchone())

    async def test_textless_fanart_background_is_preferred_and_cached(self) -> None:
        settings.fanart_api_key = "fanart-test"
        item_id = self.items["missing"]
        payload = {"moviebackground": [
            {"url": "https://assets.fanart.tv/fanart/movie-en-5213.jpg", "lang": "en", "likes": "9"},
            {"url": "https://assets.fanart.tv/fanart/movie-bg-5214.jpg", "lang": "", "likes": "2"},
            {"url": "https://evil.example/bg.jpg", "lang": "", "likes": "99"},
        ]}
        request = httpx.Request("GET", "https://webservice.fanart.tv/v3/movies/102")
        lookup = AsyncMock(return_value=httpx.Response(200, json=payload, request=request))
        image = AsyncMock(return_value=(PNG, "image/png"))
        with patch("app.clients.FanartClient._movie", new=lookup), patch("app.api.films.FanartClient.background_image", new=image):
            first = await film_routes.playlist_item_backdrop(item_id)
            await film_routes.playlist_item_backdrop(item_id)
        self.assertEqual(first.body, PNG)
        image.assert_awaited_once_with("https://assets.fanart.tv/fanart/movie-bg-5214.jpg")
        self.assertEqual(self._row(item_id)[0], "https://assets.fanart.tv/fanart/movie-bg-5214.jpg")
        detail = await film_routes.film_detail(item_id)
        self.assertIn(f"/backdrop?v={film_service.fanart_version('https://assets.fanart.tv/fanart/movie-bg-5214.jpg')}", detail["backdrop_url"])

    async def test_falls_back_to_tmdb_and_hides_when_nothing_exists(self) -> None:
        settings.fanart_api_key = "fanart-test"
        settings.tmdb_api_key = "tmdb-test"
        item_id = self.items["missing"]
        empty = httpx.Response(200, json={}, request=httpx.Request("GET", "https://webservice.fanart.tv/v3/movies/102"))
        tmdb_image = AsyncMock(return_value=(PNG, "image/png"))
        with patch("app.clients.FanartClient._movie", new=AsyncMock(return_value=empty)), \
             patch("app.api.films.TMDBClient.movie_details", new=AsyncMock(return_value={"backdrop_path": "/still.jpg"})), \
             patch("app.api.films.TMDBClient.poster_image", new=tmdb_image):
            response = await film_routes.playlist_item_backdrop(item_id)
        self.assertEqual(response.body, PNG)
        tmdb_image.assert_awaited_once_with("/still.jpg", size="w1280")
        self.assertEqual(self._row(item_id), ("", "/still.jpg"))
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET tmdb_backdrop_path='' WHERE id=?", (item_id,))
        self.assertIsNone((await film_routes.film_detail(item_id))["backdrop_url"])
        with self.assertRaises(HTTPException) as missing:
            await film_routes.playlist_item_backdrop(item_id)
        self.assertEqual(missing.exception.status_code, 404)

    async def test_reidentification_clears_backdrop_choice(self) -> None:
        item_id = self.items["missing"]
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET fanart_backdrop_url='x',tmdb_backdrop_path='/y.jpg' WHERE id=?", (item_id,))
        film_service.reidentify_item(item_id, {"id": 999, "title": "T", "original_title": "T", "release_date": "2001-01-01"})
        self.assertEqual(self._row(item_id), (None, None))
        self.assertTrue(is_signed_media_path(f"/api/playlist-items/{item_id}/backdrop"))


class ImdbFirstRecognitionTests(FilmFixture):
    """IMDb 编号换算 TMDB 最可靠：与来源 TMDB 编号并存时优先 IMDb；校准只采信按编号得到的结果。"""

    async def test_imdb_conversion_wins_over_source_tmdb_id(self) -> None:
        from app.services import recognition

        tmdb = AsyncMock()
        tmdb.find_by_imdb.return_value = [{"id": 36095, "title": "X圣治", "original_title": "キュア", "release_date": "1997-12-27"}]
        with patch.object(recognition, "TMDBClient", return_value=tmdb):
            media = await recognition.recognize_movie("Cure", 1997, "tt0123948", 1199410)
        self.assertEqual((media["id"], media["matched_by"]), (36095, "imdb"))
        tmdb.movie_details.assert_not_awaited()

    async def test_letterboxd_is_fetched_when_imdb_missing_even_with_source_tmdb(self) -> None:
        from app.services import recognition

        row = {"id": self.items["missing"], "original_title": "Cure", "year": 1997, "imdb_id": None,
               "source_tmdb_id": 36095, "source_ref": "letterboxd:cure"}
        lookup = AsyncMock(return_value=(36095, "tt0123948"))
        recognize = AsyncMock(return_value=None)
        with patch("app.list_sources.PlaylistSourceFetcher.letterboxd_film_ids", new=lookup), \
             patch.object(recognition, "recognize_movie", new=recognize):
            await recognition.recognize_item(row)
        recognize.assert_awaited_once_with("Cure", 1997, "tt0123948", 36095)

    async def _verify(self, media_by_title: dict[str, dict]) -> dict:
        from app.services import automation

        settings.tmdb_api_key = "tmdb-test"

        async def fake_recognize(item):
            return media_by_title.get(item["original_title"])

        with connect() as conn:
            task_id = to_int(conn.execute(
                """INSERT INTO recognition_tasks(playlist_id,status,total,created_at,updated_at,mode)
                   VALUES(?,?,?,?,?,?)""", (self.playlist_id, "queued", 10, utc_now(), utc_now(), "verify"),
            ).lastrowid)
        with patch.object(automation, "recognize_item", new=fake_recognize), \
             patch.object(automation, "_trigger_post_recognition_library_scan"):
            await automation.run_recognition(task_id)
        with connect() as conn:
            return dict(conn.execute("SELECT * FROM recognition_tasks WHERE id=?", (task_id,)).fetchone())

    async def test_verify_corrects_only_id_based_mismatches(self) -> None:
        base = {"title": "新", "original_title": "New", "release_date": "2001-01-01"}
        task = await self._verify({
            "Movie missing": {**base, "id": 9001, "matched_by": "imdb"},
            "Movie candidates": {**base, "id": 9002, "matched_by": "title"},
        })
        self.assertEqual((task["status"], task["corrected"]), ("partial", 1))
        with connect() as conn:
            fixed = conn.execute("SELECT tmdb_id,library_state FROM playlist_items WHERE id=?", (self.items["missing"],)).fetchone()
            kept = conn.execute("SELECT tmdb_id FROM playlist_items WHERE id=?", (self.items["candidates"],)).fetchone()
        # 按 IMDb 得到不同结果：改正并重新核对 Emby；片名搜索得到的不同结果不推翻已有识别。
        self.assertEqual(tuple(fixed), (9001, "unknown"))
        self.assertEqual(kept[0], 105)


class NexusSearchTests(IsolatedAppTestCase):
    """线上站点实测后的搜索修正：IMDb 搜索范围、表单下载链接、听听歌的链接与参数、跳转页与连接检测。"""

    HDSKY_ROW = (
        '<table class="torrents"><tr><td><a href="userdetails.php?id=99412">me</a></td></tr>'
        '<tr><td class="rowfollow"><table><tr><td><a href="details.php?id=624930&hit=1" title="Casablanca 1942 1080p BluRay x265-FRDS">'
        'Casablanca 1942 1080p BluRay x265-FRDS</a></td><td><form action="download.php?id=624930&t=1&sign=abc" method="POST"></form></td>'
        '</tr></table></td><td>0</td><td>2月</td><td>22.61 GB</td><td>3</td><td>0</td><td>22</td><td>0%</td><td>匿名</td></tr></table>'
    )
    TTG_ROW = (
        '<table id="torrent_table"><tr><td></td><td><a href="/t/835105/"><b>Casablanca 1942 1080p BluRay x265-FRDS</b></a>'
        '<a href="/dl/835105/1433">dl</a><a href="/details.php?id=835105&hit=1&filelist=1">3</a></td>'
        '<td>3</td><td>0</td><td>2026-09-26</td><td>21 小时</td><td>12.30 GB</td><td>469 次</td><td>306 / 6</td><td>[匿名用户]</td></tr></table>'
    )

    async def _search(self, html: str, base: str, path: str, title: str = "Casablanca", imdb: str | None = None):
        from app.clients import NexusPHPClient

        response = httpx.Response(200, text=html, request=httpx.Request("GET", base.rstrip("/") + path))
        request = AsyncMock(return_value=response)
        with patch("app.clients.safe_request", new=request):
            rows = await NexusPHPClient().search({"name": "站", "base_url": base, "cookie": "c"}, title, imdb)
        return rows, request.await_args.kwargs["params"]

    async def test_imdb_query_uses_imdb_search_area(self) -> None:
        _, params = await self._search(self.HDSKY_ROW, "https://hdsky.me/", "/torrents.php", imdb="tt0034583")
        self.assertEqual(params, {"search": "tt0034583", "search_area": 4})
        _, params = await self._search(self.HDSKY_ROW, "https://hdsky.me/", "/torrents.php")
        self.assertEqual(params, {"search": "Casablanca", "search_area": 0})

    async def test_form_download_and_userdetails_are_handled(self) -> None:
        rows, _ = await self._search(self.HDSKY_ROW, "https://hdsky.me/", "/torrents.php")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["title"], rows[0]["seeders"]), ("Casablanca 1942 1080p BluRay x265-FRDS", 3))
        self.assertTrue(rows[0]["enclosure"].startswith("https://hdsky.me/download.php?id=624930"))

    async def test_totheglory_links_params_and_paired_seeders(self) -> None:
        rows, params = await self._search(self.TTG_ROW, "https://totheglory.im/", "/browse.php", imdb="tt0034583")
        # 听听歌不支持按 IMDb 搜索：改用片名，参数名为 search_field。
        self.assertEqual(params, {"search_field": "Casablanca", "search_area": 0})
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["title"], rows[0]["seeders"]), ("Casablanca 1942 1080p BluRay x265-FRDS", 306))
        self.assertEqual(rows[0]["enclosure"], "https://totheglory.im/dl/835105/1433")

    async def test_two_factor_and_maintenance_redirects_are_errors(self) -> None:
        for path, expected in (("/take2fa.php", "二次验证"), ("/claim/", "维护")):
            with self.assertRaisesRegex(RuntimeError, expected):
                await self._search("<html></html>", "https://site.example/", path)

    async def test_connection_check_runs_a_real_search(self) -> None:
        from app.clients import NexusPHPClient

        with patch.object(NexusPHPClient, "search", new=AsyncMock(return_value=[])) as search:
            result = await NexusPHPClient().check({"name": "站"})
        # 先按 IMDb 搜，搜不到再按片名搜；两次都没有结果才算“搜不到”。
        self.assertEqual(
            [call.args for call in search.await_args_list],
            [({"name": "站"}, "The Godfather", "tt0068646"), ({"name": "站"}, "The Godfather 1972")],
        )
        self.assertTrue(result["empty"])
        with patch.object(NexusPHPClient, "search", new=AsyncMock(return_value=[{"title": "x"}])):
            result = await NexusPHPClient().check({"name": "站"})
        self.assertNotIn("empty", result)

    async def test_empty_search_is_recorded_as_its_own_status(self) -> None:
        from app.clients import NexusPHPClient
        from app.services.sites import test_site_config

        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at) VALUES(?,?,?,?,?,?)",
                ("音乐站", "nexusphp", "https://music.example", 1, 1, utc_now()),
            ).lastrowid)
        with patch.object(NexusPHPClient, "check", new=AsyncMock(return_value={"ok": True, "empty": True, "message": "搜不到"})):
            result = await test_site_config({"id": site_id, "name": "音乐站", "adapter": "nexusphp"})
        self.assertEqual((result["status"], result["ok"]), ("empty", False))


class CookieProvenanceTests(IsolatedAppTestCase):
    """Cookie 的来源与更新时间、最近一次同步摘要，以及 Cookie 变化后的后台复测。"""

    def _site(self, name: str, base: str, cookie: str = "old") -> int:
        with connect() as conn:
            return to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,enabled,search_enabled,created_at) VALUES(?,?,?,?,?,?,?)",
                (name, "nexusphp", base, cookie, 1, 1, utc_now()),
            ).lastrowid)

    async def test_sync_records_source_summary_and_retests_changed_sites(self) -> None:
        from app import state as app_state
        from app.services import sites as site_service

        changed = self._site("春天", "https://springsunday.net")
        same = self._site("家园", "https://hdhome.org", cookie="same")
        self._site("皇后", "https://open.cd")
        groups = {"springsunday.net": "new", "hdhome.org": "same"}
        with patch.object(site_service, "test_site_config", new=AsyncMock()) as retest:
            result = site_service.apply_cookie_groups(groups, origin="pull")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.assertEqual((result["updated"], result["missing"]), (["春天"], ["皇后"]))
        with connect() as conn:
            rows = {r["id"]: r for r in conn.execute("SELECT id,cookie_source,cookie_updated_at FROM pt_sites")}
        self.assertEqual(rows[changed]["cookie_source"], "cookiecloud")
        self.assertIsNotNone(rows[changed]["cookie_updated_at"])
        self.assertIsNone(rows[same]["cookie_source"])
        summary = app_state.last_cookie_sync
        self.assertEqual((summary["origin"], summary["updated"], summary["unchanged"], summary["missing"]), ("pull", ["春天"], 1, ["皇后"]))
        # 只复测 Cookie 有变化的站点。
        self.assertEqual([call.args[0]["id"] for call in retest.await_args_list], [changed])
        status = await system_routes.cookiecloud_status()
        self.assertEqual(status["last_sync"]["origin"], "pull")

    async def test_manual_cookie_edit_marks_source(self) -> None:
        from app.main import app

        site_id = self._site("春天", "https://springsunday.net")
        body = {"name": "春天", "base_url": "https://springsunday.net", "cookie": "typed-by-user"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(f"/api/sites/{site_id}", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        with connect() as conn:
            row = conn.execute("SELECT cookie_source,cookie_updated_at FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        self.assertEqual(row["cookie_source"], "manual")
        self.assertIsNotNone(row["cookie_updated_at"])
