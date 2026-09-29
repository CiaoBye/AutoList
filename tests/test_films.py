"""影片：以影片为中心的状态计算、海报与剧照、藏馆 / 挑选 / 动态接口与身份修正。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app.api import films as film_routes
from app.api import home as home_routes
from app.api import images as image_routes
from app.api import picks as pick_routes
from app.api import system as system_routes
from app.api import timeline as timeline_routes
from app.api.playlists import playlist_item_poster
from app.clients import EmbyClient
from app.config import settings
from app.database import SCHEMA_VERSION, connect
from app.security import is_signed_media_path
from app.services import films as film_service
from app.services.recognition import persist_tmdb_item
from app.util import to_int, utc_now
from tests.support import FilmFixture, IsolatedAppTestCase, SeededPlaylistTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


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
        self.assertEqual(selected["candidates"][0]["in_selection"], 1)
        self.assertFalse(selected["candidates"][0]["context_available"])
        with self.assertRaises(HTTPException) as missing:
            await film_routes.film_detail(99999)
        self.assertEqual(missing.exception.status_code, 404)

    async def test_home_summarises_todos_and_recent(self) -> None:
        with self._no_transmission(), patch("app.api.home.hydrate_recent_emby_posters", new=AsyncMock()):
            home = await home_routes.home(playlist_id=None)
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
        with patch("app.api.images.TMDBClient.movie_details", new=details), \
             patch("app.api.images.TMDBClient.poster_image", new=image):
            first = await image_routes.playlist_item_tmdb_poster(item_id)
            second = await image_routes.playlist_item_tmdb_poster(item_id)
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
        with patch("app.api.images.TMDBClient.movie_details", new=AsyncMock(return_value={"poster_path": "https://evil.example/x.jpg"})):
            with self.assertRaises(HTTPException) as rejected:
                await image_routes.playlist_item_tmdb_poster(item_id)
        self.assertEqual(rejected.exception.status_code, 404)
        with connect() as conn:
            stored = conn.execute("SELECT tmdb_poster_path FROM playlist_items WHERE id=?", (item_id,)).fetchone()[0]
        # 记住“没有海报”，之后不再重复请求 TMDB，列表也不再生成海报地址。
        self.assertEqual(stored, "")
        result = await self._films(status="candidates")
        self.assertIsNone(result["items"][0]["poster_url"])
        with self.assertRaises(HTTPException) as unrecognized:
            await image_routes.playlist_item_tmdb_poster(self.items["unrecognized"])
        self.assertEqual(unrecognized.exception.status_code, 404)

    async def test_poster_endpoint_without_tmdb_key_is_not_found(self) -> None:
        settings.tmdb_api_key = ""
        with self.assertRaises(HTTPException) as raised:
            await image_routes.playlist_item_tmdb_poster(self.items["missing"])
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
             patch("app.api.images.FanartClient.poster_image", new=image):
            first = await image_routes.playlist_item_fanart_poster(item_id)
            second = await image_routes.playlist_item_fanart_poster(item_id)
        self.assertEqual((first.status_code, first.media_type, second.body), (200, "image/png", PNG))
        # 未带版本号（待定地址）只短期缓存；带上当前海报的版本号才长期缓存。
        self.assertEqual(first.headers["Cache-Control"], image_routes.FALLBACK_POSTER_CACHE)
        versioned = await image_routes.playlist_item_fanart_poster(item_id, film_service.fanart_version(FANART_URL))
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
        tmdb = AsyncMock(return_value=image_routes.Response(content=PNG, media_type="image/png"))
        with patch("app.api.images.TMDBClient.movie_details", new=details), \
             patch("app.api.images.FanartClient.movie_poster_url", new=posters), \
             patch("app.api.images.playlist_item_tmdb_poster", new=tmdb):
            await image_routes.playlist_item_fanart_poster(item_id)
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
        tmdb = AsyncMock(return_value=image_routes.Response(content=PNG, media_type="image/png"))
        with patch("app.clients.FanartClient._movie", new=AsyncMock(return_value=httpx.Response(404, json={}))), \
             patch("app.api.images.playlist_item_tmdb_poster", new=tmdb):
            response = await image_routes.playlist_item_fanart_poster(item_id)
        self.assertEqual(response.body, PNG)
        self.assertEqual(response.headers["Cache-Control"], image_routes.FALLBACK_POSTER_CACHE)
        self.assertEqual(self._fanart_row(item_id), "")

    async def test_lookup_failure_falls_back_without_remembering(self) -> None:
        settings.fanart_api_key = "fanart-test"
        settings.tmdb_api_key = "tmdb-test"
        item_id = self.items["missing"]
        tmdb = AsyncMock(return_value=image_routes.Response(content=PNG, media_type="image/png"))
        with patch("app.clients.FanartClient._movie", new=AsyncMock(side_effect=httpx.ConnectError("down"))), \
             patch("app.api.images.playlist_item_tmdb_poster", new=tmdb):
            response = await image_routes.playlist_item_fanart_poster(item_id)
        self.assertEqual(response.body, PNG)
        self.assertIsNone(self._fanart_row(item_id))
        with self.assertRaises(HTTPException) as unrecognized:
            await image_routes.playlist_item_fanart_poster(self.items["unrecognized"])
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
            queue = await pick_routes.picks(playlist_id=self.playlist_id, status="all")
        buckets = {item["id"]: item["bucket"] for item in queue["items"]}
        self.assertEqual(queue["counts"], {"all": 3, "candidates": 1, "selected": 1, "no_eligible": 1})
        self.assertEqual(buckets[self.items["candidates"]], "candidates")
        self.assertEqual(buckets[self.items["selected"]], "selected")
        self.assertEqual(buckets[self.items["no_eligible"]], "no_eligible")
        by_id = {item["id"]: item for item in queue["items"]}
        self.assertTrue(by_id[self.items["candidates"]]["candidates"][0]["context_available"])
        self.assertEqual(by_id[self.items["selected"]]["candidates"][0]["in_selection"], 1)
        self.assertEqual(by_id[self.items["no_eligible"]]["candidates"], [])
        self.assertEqual(by_id[self.items["no_eligible"]]["excluded_count"], 1)
        with self._no_transmission():
            selected = await pick_routes.picks(playlist_id=self.playlist_id, status="selected")
        self.assertEqual([item["id"] for item in selected["items"]], [self.items["selected"]])
        with self.assertRaises(HTTPException) as invalid:
            await pick_routes.picks(playlist_id=None, status="bogus")
        self.assertEqual(invalid.exception.status_code, 422)


class TimelineTests(FilmFixture):
    async def test_timeline_merges_sources_and_filters(self) -> None:
        with patch("app.api.timeline.log_events", new=AsyncMock(return_value=[
            {"ts": "2099-01-01T00:00:00+00:00", "level": "INFO", "event": "playlist_imported", "detail": "导入片单【测试】"},
            {"ts": "2099-01-01T00:00:01+00:00", "level": "INFO", "event": "cookiecloud_sync", "detail": "不应出现"},
        ])):
            everything = await timeline_routes.timeline(type="all", film_id=None, limit=120)
            films = await timeline_routes.timeline(type="films", film_id=None, limit=120)
            system = await timeline_routes.timeline(type="system", film_id=None, limit=120)
            one = await timeline_routes.timeline(type="all", film_id=self.items["selected"], limit=120)
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
            await timeline_routes.timeline(type="bogus", film_id=None, limit=10)
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
        with patch("app.clients.FanartClient._movie", new=lookup), patch("app.api.images.FanartClient.background_image", new=image):
            first = await image_routes.playlist_item_backdrop(item_id)
            await image_routes.playlist_item_backdrop(item_id)
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
             patch("app.api.images.TMDBClient.movie_details", new=AsyncMock(return_value={"backdrop_path": "/still.jpg"})), \
             patch("app.api.images.TMDBClient.poster_image", new=tmdb_image):
            response = await image_routes.playlist_item_backdrop(item_id)
        self.assertEqual(response.body, PNG)
        tmdb_image.assert_awaited_once_with("/still.jpg", size="w1280")
        self.assertEqual(self._row(item_id), ("", "/still.jpg"))
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET tmdb_backdrop_path='' WHERE id=?", (item_id,))
        self.assertIsNone((await film_routes.film_detail(item_id))["backdrop_url"])
        with self.assertRaises(HTTPException) as missing:
            await image_routes.playlist_item_backdrop(item_id)
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


class EmbyPosterTests(SeededPlaylistTestCase):
    async def test_emby_poster_is_proxied_and_validated(self) -> None:
        with connect() as conn:
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE playlist_items SET emby_item_id='abc123',emby_image_tag='tag1' WHERE id=?", (item_id,),
            )
        original = EmbyClient.poster

        async def fake_poster(_client: object, _item_id: str) -> tuple[bytes, str]:
            return b"\x89PNG\r\n\x1a\nposter", "image/png"

        EmbyClient.poster = fake_poster
        try:
            response = await playlist_item_poster(to_int(item_id), "tag1")
        finally:
            EmbyClient.poster = original
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.body, b"\x89PNG\r\n\x1a\nposter")
