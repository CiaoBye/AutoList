"""升级搜索（洗版）：已入馆影片只保留画质更高的候选，选定后提交不被“已入库”拦下。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api import films as film_routes
from app.clients.emby import quality_tier
from app.config import settings
from app.schemas import FilmsUpgradePayload
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
        # 有合格升级候选的已入馆影片在书目里标“有升级候选”。
        with patch("app.services.films._current_downloads_cached", new=AsyncMock(return_value=([], "known_empty"))):
            page = await film_routes.films(playlist_id=playlist_id, status="issue:upgrade_ready", q="", page=1, page_size=60)
        self.assertEqual([film["id"] for film in page["items"]], [item_id])
        # 遴选里“升级”分类只列出更高画质的候选，不含“不高于现有”的排除项。
        from app.api.picks import picks

        with patch("app.services.films._current_downloads_cached", new=AsyncMock(return_value=([], "known_empty"))), \
             patch("app.api.picks.EmbyClient.versions", new=AsyncMock(return_value=versions)):
            pick_page = await picks(playlist_id=playlist_id, status="upgrade")
        self.assertEqual(
            pick_page["items"][0]["current"], [{"kind": "library", "name": "a.mkv", "size": 1, "resolution": "1080p"}],
        )
        self.assertEqual([(film["id"], film["bucket"]) for film in pick_page["items"]], [(item_id, "upgrade")])
        self.assertEqual({candidate["title"] for candidate in pick_page["items"][0]["candidates"]}, {GOOD})
        self.assertEqual(pick_page["counts"]["upgrade"], 1)
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




class UpgradeMarkersTests(UpgradeSearchTests):
    """书目里的“可升级”标记与批量升级。"""

    def _tier(self, item_id: int, tier: int | None) -> None:
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET library_tier=? WHERE id=?", (tier, item_id))

    async def _page(self, playlist_id: int, status: str) -> dict:
        with patch("app.services.films._current_downloads_cached", new=AsyncMock(return_value=([], "known_empty"))):
            return await film_routes.films(playlist_id=playlist_id, status=status, q="", page=1, page_size=60)

    async def test_only_films_below_2160_are_upgradable(self) -> None:
        playlist_id, item_id = self._film()
        self._tier(item_id, 1080)
        page = await self._page(playlist_id, "all")
        self.assertEqual(page["items"][0]["issues"], ["upgradable"])
        self.assertEqual(page["counts"]["issue:upgradable"], 1)
        self._tier(item_id, 2160)
        self.assertEqual((await self._page(playlist_id, "all"))["items"][0]["issues"], [])
        self._tier(item_id, None)  # 还不知道画质：不标
        self.assertEqual((await self._page(playlist_id, "all"))["items"][0]["issues"], [])

    async def test_batch_groups_by_tier_skips_unsuitable_and_hides_searched_films(self) -> None:
        settings.emby_base_url, settings.emby_api_key = "http://emby.test", "k"
        playlist_id, first = self._film()
        with connect() as conn:
            def add(rank: int, title: str, state: str = "in_library") -> int:
                return to_int(conn.execute(
                    "INSERT INTO playlist_items(playlist_id,rank_no,original_title,year,library_state) VALUES(?,?,?,?,?)",
                    (playlist_id, rank, title, 2000 + rank, state),
                ).lastrowid)

            second, ready, missing = add(2, "B"), add(3, "C"), add(4, "D", "not_found")
        self._tier(first, 1080)
        self._tier(second, 720)
        self._tier(ready, 2160)
        with patch("app.api.films.SEARCH.start"), patch("app.api.films.run_search", new=lambda _id: None):
            result = await film_routes.upgrade_search_films(FilmsUpgradePayload(ids=[first, second, ready, missing, 99999]))
        self.assertEqual(result["started"], 2)
        self.assertEqual(len(result["task_ids"]), 2)  # 1080 与 720 各一个任务
        self.assertEqual({row["reason"] for row in result["skipped"]}, {"已经是 2160p", "还没有入馆", "影片不存在"})
        with connect() as conn:
            tiers = sorted(row[0] for row in conn.execute("SELECT upgrade_above FROM search_tasks WHERE trigger='upgrade'"))
        self.assertEqual(tiers, [720, 1080])
        # 刚搜过升级的影片 30 天内不再标“可升级”。
        page = await self._page(playlist_id, "all")
        self.assertEqual([film["issues"] for film in page["items"] if film["id"] in (first, second)], [[], []])

    def test_payload_limits_batch_to_20(self) -> None:
        with self.assertRaises(ValueError):
            FilmsUpgradePayload(ids=list(range(1, 22)))


class StalledCurrentDownloadTests(UpgradeSearchTests):
    async def test_stalled_film_lists_its_original_download(self) -> None:
        from app.api.picks import _current_releases

        _playlist_id, item_id = self._film("not_found")
        with connect() as conn:
            conn.execute(
                "INSERT INTO download_history(playlist_item_id,title,torrent_name,site_name,submission_hash,success,created_at) VALUES(?,?,?,?,?,1,?)",
                (item_id, "t", "n", "彩虹岛", "abc123", utc_now()),
            )
        torrent = {"hashString": "ABC123", "name": "Movie.2020.1080p", "totalSize": 5 * 1024**3, "percentDone": 0.25,
                   "status": 4, "isStalled": True, "activityDate": 0, "addedDate": 1}
        with patch("app.api.picks._current_downloads_cached", new=AsyncMock(return_value=([torrent], "known_present"))):
            current = await _current_releases([{"id": item_id, "bucket": "stalled"}])
        (release,) = current[item_id]
        self.assertEqual((release["kind"], release["name"], release["site"], release["percent"]), ("download", "Movie.2020.1080p", "彩虹岛", 25.0))
        self.assertGreater(release["idle_hours"], 24)
        self.assertIsInstance(release["idle_hours"], int)  # 响应模型要求整数小时
        from app.responses import CurrentRelease

        CurrentRelease(**release)  # 能通过响应模型校验


class UpgradeSwitchTests(UpgradeSearchTests):
    async def test_switch_off_hides_markers_and_rejects_upgrade_searches(self) -> None:
        playlist_id, item_id = self._film()
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET library_tier=1080 WHERE id=?", (item_id,))
        settings.upgrade_enabled = False
        with patch("app.services.films._current_downloads_cached", new=AsyncMock(return_value=([], "known_empty"))):
            page = await film_routes.films(playlist_id=playlist_id, status="all", q="", page=1, page_size=60)
        self.assertEqual(page["items"][0]["issues"], [])
        for call in (film_routes.upgrade_search_film(item_id), film_routes.upgrade_search_films(FilmsUpgradePayload(ids=[item_id]))):
            with self.assertRaises(HTTPException) as caught:
                await call
            self.assertEqual(caught.exception.status_code, 409)
        settings.upgrade_enabled = True
        with patch("app.services.films._current_downloads_cached", new=AsyncMock(return_value=([], "known_empty"))):
            page = await film_routes.films(playlist_id=playlist_id, status="all", q="", page=1, page_size=60)
        self.assertEqual(page["items"][0]["issues"], ["upgradable"])

    def test_original_download_resolution_floor(self) -> None:
        from app.api.picks import _original_height

        releases = [{"kind": "download", "name": "The.Big.Lebowski.1998.2160p.UHD.Blu-ray.x265"}, {"kind": "library", "name": "x.1080p.mkv"}]
        self.assertEqual(_original_height(releases), 2160)
        self.assertIsNone(_original_height([{"kind": "download", "name": "Movie.DVDRip"}]))
