"""站况：Cookie 健康、连续失败、近期统计与 Transmission 做种汇总。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.api import site_status as status_routes
from app.database import connect
from app.services.site_health import cookie_health
from app.util import utc_now
from tests.support import SeededPlaylistTestCase


class SiteStatusTests(SeededPlaylistTestCase):
    def _site(self, name: str, **values: object) -> int:
        columns = {"name": name, "adapter": "nexusphp", "base_url": "https://x.test", "cookie": "c=1", "created_at": utc_now(), **values}
        with connect() as conn:
            return conn.execute(
                f"INSERT INTO pt_sites({','.join(columns)}) VALUES({','.join('?' * len(columns))})", tuple(columns.values()),
            ).lastrowid  # type: ignore[return-value]

    def _attempt(self, site_id: int, name: str, status: str, message: str | None = None) -> None:
        with connect() as conn:
            conn.execute(
                """INSERT INTO search_attempts(task_id,playlist_item_id,site_id,site_name,status,result_count,duration_ms,
                   error_message,created_at,finished_at) VALUES(?,?,?,?,?,?,100,?,?,?)""",
                (self.task_id, self.item_id, site_id, name, status, 3 if status == "success" else 0, message, utc_now(), utc_now()),
            )

    async def _page(self, torrents: list[dict] | None = None, state: str = "known_present") -> dict:
        snapshot = AsyncMock(return_value=(torrents or [], state))
        with patch("app.services.site_health._current_downloads_cached", new=snapshot):
            return await status_routes.site_status()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        with connect() as conn:
            self.item_id = conn.execute("SELECT id FROM playlist_items ORDER BY id LIMIT 1").fetchone()[0]
            self.task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,1,1,'completed',1,?,?)""", (self.playlist_id, utc_now(), utc_now()),
            ).lastrowid

    async def test_healthy_and_failing_and_expired_sites(self) -> None:
        good = self._site("好站")
        bad = self._site("连败站")
        gone = self._site("失效站")
        for _ in range(2):
            self._attempt(good, "好站", "success")
        self._attempt(bad, "连败站", "success")
        for _ in range(3):
            self._attempt(bad, "连败站", "failed", "超时")
        self._attempt(gone, "失效站", "failed", "Cookie 已失效，站点返回登录页面")
        rows = {row["name"]: row for row in (await self._page())["sites"]}
        self.assertEqual(rows["好站"]["cookie"]["state"], "ok")
        self.assertEqual(rows["好站"]["search"]["success_rate"], 100.0)
        self.assertEqual((rows["连败站"]["cookie"]["state"], rows["连败站"]["failure_streak"]), ("failing", 3))
        self.assertEqual(rows["失效站"]["cookie"]["state"], "expired")
        page = await self._page()
        self.assertEqual(page["problem_count"], 2)
        self.assertEqual(len(rows["好站"]["history"]), 14)
        self.assertEqual(rows["好站"]["history"][-1]["total"], 2)

    async def test_missing_cookie_and_api_key_sites(self) -> None:
        self._site("没填", cookie="")
        self._site("接口站", adapter="mteam", cookie="", api_key="k")
        rows = {row["name"]: row for row in (await self._page())["sites"]}
        self.assertEqual(rows["没填"]["cookie"]["state"], "missing")
        self.assertEqual(rows["接口站"]["cookie"]["state"], "none")

    async def test_disabled_site_is_not_a_problem(self) -> None:
        site = self._site("停用站", enabled=0)
        self._attempt(site, "停用站", "failed", "Cookie 已失效")
        page = await self._page()
        self.assertEqual(page["problem_count"], 0)

    async def test_seeding_is_summed_by_site_label_and_hidden_when_transmission_unknown(self) -> None:
        self._site("春天")
        with connect() as conn:
            conn.execute(
                "INSERT INTO download_history(title,torrent_name,site_name,submission_hash,success,created_at) VALUES('t','n','春天','abc123',1,?)",
                (utc_now(),),
            )
        torrents = [
            {"labels": [], "totalSize": 10, "hashString": "ABC123"},
            {"labels": ["MOVIEPILOT", "春天"], "totalSize": 5},
            {"labels": ["其他"], "totalSize": 99},
        ]
        row = (await self._page(torrents))["sites"][0]
        self.assertEqual(row["seeding"], {"count": 2, "size": 15})
        page = await self._page([], state="unknown")
        self.assertIsNone(page["sites"][0]["seeding"])
        self.assertFalse(page["transmission_available"])

    async def test_submission_counts_come_from_history(self) -> None:
        self._site("春天")
        with connect() as conn:
            for success in (1, 1, 0):
                conn.execute(
                    "INSERT INTO download_history(title,torrent_name,site_name,success,created_at) VALUES('t','n','春天',?,?)",
                    (success, utc_now()),
                )
        row = (await self._page())["sites"][0]
        self.assertEqual(row["submissions"], {"total": 3, "succeeded": 2, "success_rate": 66.7})

    def test_cookie_health_text_for_ok(self) -> None:
        self.assertEqual(cookie_health({"cookie": "c", "adapter": "nexusphp"}, 0, False)["state"], "ok")
