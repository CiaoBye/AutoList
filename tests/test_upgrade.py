"""升级搜索（洗版）：已入馆影片只保留画质更高的候选，选定后提交不被“已入库”拦下。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api import films as film_routes
from app.clients.emby import quality_tier
from app.config import settings
from app.database import connect
from app.queries import selection as selection_queries
from app.services.search import apply_upgrade_floor, run_search
from app.util import to_int, utc_now
from tests.support import IsolatedAppTestCase

GOOD = "Workflow.Movie.2020.2160p.BluRay.x265-FRDS"
SAME = "Workflow.Movie.2020.1080p.BluRay.x264-FRDS"


class UpgradeFloorTests(IsolatedAppTestCase):
    def test_floor_marks_non_higher_resolutions_excluded(self) -> None:
        eligible = {"eligible": True, "manual": False, "recommendation": "recommended", "resolution": "1080p", "reason": "ok"}
        self.assertTrue(apply_upgrade_floor(eligible, None)["eligible"])
        lowered = apply_upgrade_floor(eligible, 1080)
        self.assertFalse(lowered["eligible"])
        self.assertIn("不高于现有的 1080p", lowered["exclusion_reason"])
        self.assertTrue(apply_upgrade_floor({**eligible, "resolution": "2160p"}, 1080)["eligible"])
        self.assertFalse(apply_upgrade_floor({**eligible, "resolution": "其他"}, 1080)["eligible"])

    def test_quality_tier_uses_width_for_scope_films(self) -> None:
        self.assertEqual(quality_tier(3840, 1600), 2160)  # 2.39:1 的 4K，高度只有 1600
        self.assertEqual(quality_tier(1920, 800), 1080)
        self.assertEqual(quality_tier(1280, 720), 720)
        self.assertEqual(quality_tier(720, 480), 480)
        self.assertIsNone(quality_tier(None, None))


class EmbyVersionsTests(IsolatedAppTestCase):
    async def test_strm_placeholder_and_real_item_merge_into_one_version_with_streams(self) -> None:
        from unittest.mock import MagicMock

        from app.clients import EmbyClient

        settings.emby_base_url, settings.emby_api_key = "http://emby.test", "k"
        mkv = "/Media/Filmlink/Casablanca (1943) - 2160p.mkv"
        payload = {"Items": [
            {"Path": "/Media/Filmlist/x.strm", "MediaSources": [{"Path": mkv, "MediaStreams": []}]},
            {"Path": mkv, "MediaSources": [{"Path": mkv, "Size": 123, "MediaStreams": [{"Type": "Video", "Width": 3840, "Height": 1600}]}]},
        ]}
        response = MagicMock()
        response.json.return_value = payload
        with patch("app.clients.emby.safe_request", new=AsyncMock(return_value=response)):
            versions = await EmbyClient().versions(289)
        self.assertEqual(versions, [{"name": "Casablanca (1943) - 2160p.mkv", "size": 123, "resolution": "3840×1600", "tier": 2160}])


class UpgradeSearchTests(IsolatedAppTestCase):
    def _film(self, library_state: str = "in_library") -> tuple[int, int]:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES('升级',1,?)", (utc_now(),),
            ).lastrowid)
            item_id = to_int(conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,library_state)
                   VALUES(?,?,?,?,?,?,?)""",
                (playlist_id, 1, "tt1234567", "Workflow Movie", 2020, "工作流电影", library_state),
            ).lastrowid)
            conn.execute(
                "INSERT OR IGNORE INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at) VALUES('S','nexusphp','https://s.example',1,1,?)",
                (utc_now(),),
            )
        return playlist_id, item_id

    async def test_route_refuses_unsuitable_films(self) -> None:
        settings.emby_base_url, settings.emby_api_key = "http://emby.test", "k"
        _, missing = self._film("not_found")
        with self.assertRaises(HTTPException) as caught:
            await film_routes.upgrade_search_film(missing)
        self.assertEqual(caught.exception.status_code, 409)
        _, owned = self._film()
        for tiers, status in (([2160], 409), ([], 409)):
            versions = [{"name": "a.mkv", "size": 1, "resolution": None, "tier": tier} for tier in tiers]
            with patch("app.api.films.EmbyClient.versions", new=AsyncMock(return_value=versions)), self.assertRaises(HTTPException) as caught:
                await film_routes.upgrade_search_film(owned)
            self.assertEqual(caught.exception.status_code, status)

    async def test_upgrade_search_keeps_only_higher_resolutions(self) -> None:
        settings.emby_base_url, settings.emby_api_key = "http://emby.test", "k"
        playlist_id, item_id = self._film()
        versions = [{"name": "a.mkv", "size": 1, "resolution": "1920×1080", "tier": 1080}]
        with patch("app.api.films.EmbyClient.versions", new=AsyncMock(return_value=versions)), \
             patch("app.api.films.SEARCH.start") as start, patch("app.api.films.run_search", new=lambda _id: None):
            started = await film_routes.upgrade_search_film(item_id)
        start.assert_called_once()
        with connect() as conn:
            task = conn.execute("SELECT trigger,upgrade_above FROM search_tasks WHERE id=?", (started["id"],)).fetchone()
        self.assertEqual((task["trigger"], task["upgrade_above"]), ("upgrade", 1080))

        async def fake_search_site(_task_id: int, _item: object, site: dict, *_args: object, **_kwargs: object) -> tuple:
            torrents = [
                {"title": GOOD, "size": 40 * 1024**3, "seeders": 20, "imdbid": "tt1234567", "site_name": "S", "_site_id": site["id"]},
                {"title": SAME, "size": 12 * 1024**3, "seeders": 30, "imdbid": "tt1234567", "site_name": "S", "_site_id": site["id"]},
            ]
            return site, torrents, None, 1, None

        media = {"id": 91, "title": "Workflow Movie", "original_title": "Workflow Movie", "release_date": "2020-01-01", "imdb_id": "tt1234567"}
        with patch("app.services.search.recognize_item", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("in_library", "e1", "t"))), \
             patch("app.services.search.search_one_site", new=fake_search_site):
            await run_search(started["id"])
        with connect() as conn:
            rows = {row["title"]: row["eligibility"] for row in conn.execute("SELECT title,eligibility FROM candidates WHERE task_id=?", (started["id"],))}
            item = conn.execute("SELECT library_state,wait_streak FROM playlist_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual(rows, {GOOD: "eligible", SAME: "excluded"})
        self.assertEqual((item["library_state"], item["wait_streak"]), ("in_library", 0))
        # 候选带着升级标记，提交时据此不把“已入库”当作跳过理由。
        candidate_id = next(iter(conn_ids(started["id"], GOOD)))
        with connect() as conn:
            conn.execute("INSERT INTO selection_items(candidate_id,selected_at) VALUES(?,?)", (candidate_id, utc_now()))
            row = selection_queries.selection_for_submission(conn)[0]
        self.assertEqual(row["task_upgrade_above"], 1080)
        from app.api.upgrades import upgrades

        listed = await upgrades()
        self.assertEqual([(row["id"], row["from_tier"], row["candidate_count"], row["selected_count"]) for row in listed], [(item_id, 1080, 1, 1)])
        detail_ids = {candidate["title"] for candidate in (await self._detail(item_id))["candidates"]}
        self.assertIn(GOOD, detail_ids)

    async def _detail(self, item_id: int) -> dict:
        settings.emby_base_url, settings.emby_api_key = "http://emby.test", "k"
        versions = [{"name": "a.mkv", "size": 1, "resolution": "1920×1080", "tier": 1080}]
        with patch("app.api.films.EmbyClient.versions", new=AsyncMock(return_value=versions)), \
             patch("app.api.films.hydrate_recent_emby_posters", new=AsyncMock()), patch("app.services.films._current_downloads_cached", new=AsyncMock(return_value=([], "known_empty"))):
            detail = await film_routes.film_detail(item_id)
        self.assertEqual((detail["library_tier"], detail["upgrade_above"]), (1080, 1080))
        return detail


def conn_ids(task_id: int, title: str) -> list[str]:
    with connect() as conn:
        return [row["id"] for row in conn.execute("SELECT id FROM candidates WHERE task_id=? AND title=?", (task_id, title))]


