"""站点自动暂停、每天自动备份、免费促销、慢站点补缺、事件回调容错。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import Request

from app.api import sync as sync_routes
from app.config import settings
from app.database import connect
from app.logs import TRACEBACK_HEAD, TRACEBACK_TAIL, _traceback_text
from app.queries import sites as site_queries
from app.services import backups as backup_service
from app.services.search import record_search_attempt, run_search
from app.services.site_pause import AUTO_PAUSE_FAILURES, resume_after_check
from app.services.sites import test_site_config
from app.sites import profile_for
from app.sites.nexusphp import parse_page
from app.util import promotion_expired, to_int, utc_now
from tests.support import IsolatedAppTestCase, SeededPlaylistTestCase

FIXTURES = Path(__file__).parent / "fixtures" / "sites"


class SiteAutoPauseTests(SeededPlaylistTestCase):
    def _site(self, name: str = "站A") -> dict:
        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at) VALUES(?,?,?,1,1,?)",
                (name, "nexusphp", "https://a.example", utc_now()),
            ).lastrowid)
            self.item_id = to_int(conn.execute("SELECT id FROM playlist_items ORDER BY id LIMIT 1").fetchone()[0])
            self.task_id = to_int(conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,1,1,'completed',1,?,?)",
                (self.playlist_id, utc_now(), utc_now()),
            ).lastrowid)
        return {"id": site_id, "name": name}

    def _attempt(self, site: dict, status: str, *, queries: int = 1) -> None:
        record_search_attempt(self.task_id, self.item_id, site, 1, status, 0, 100, "x" if status == "failed" else None, None, query_count=queries)

    def _row(self, site: dict) -> dict:
        with connect() as conn:
            return dict(conn.execute("SELECT * FROM pt_sites WHERE id=?", (site["id"],)).fetchone())

    async def test_consecutive_failures_pause_the_site_and_skipped_attempts_do_not_count(self) -> None:
        site = self._site()
        for _ in range(AUTO_PAUSE_FAILURES - 1):
            self._attempt(site, "failed")
        self._attempt(site, "failed", queries=0)  # “本次任务已跳过”不算
        self.assertEqual(self._row(site)["search_enabled"], 1)
        self._attempt(site, "failed")
        row = self._row(site)
        self.assertEqual(row["search_enabled"], 0)
        self.assertIsNotNone(row["auto_paused_at"])
        self.assertIn(str(AUTO_PAUSE_FAILURES), row["auto_paused_reason"])
        with connect() as conn:
            self.assertEqual(site_queries.auto_paused_site_names(conn), ["站A"])

    async def test_recent_success_prevents_pausing(self) -> None:
        site = self._site()
        self._attempt(site, "success")
        for _ in range(AUTO_PAUSE_FAILURES):
            self._attempt(site, "failed")
        self.assertEqual(self._row(site)["search_enabled"], 1)  # 一小时内刚成功过：只是一阵抖动

    async def test_healthy_check_resumes_and_saving_with_search_on_clears_the_flag(self) -> None:
        site = self._site()
        for _ in range(AUTO_PAUSE_FAILURES):
            self._attempt(site, "failed")
        with patch("app.services.sites.NexusPHPClient.check", new=AsyncMock(return_value={"ok": True, "message": "ok"})), \
             patch("app.services.sites.with_cookie_refresh", new=lambda s, op: op(s)):
            result = await test_site_config({**self._row(site), "adapter": "nexusphp"})
        self.assertTrue(result["ok"])
        row = self._row(site)
        self.assertEqual((row["search_enabled"], row["auto_paused_at"]), (1, None))
        # 再次暂停后，表单里把“参与搜索”打开 = 明确选择，清除自动暂停标记。
        for _ in range(AUTO_PAUSE_FAILURES):
            self._attempt(site, "failed")
        self.assertIsNotNone(self._row(site)["auto_paused_at"])
        with connect() as conn:
            values = {column: self._row(site).get(column) for column in site_queries.SITE_FORM_COLUMNS}
            site_queries.update_site(conn, site["id"], {**values, "search_enabled": 0})
            self.assertIsNotNone(conn.execute("SELECT auto_paused_at FROM pt_sites WHERE id=?", (site["id"],)).fetchone()[0])
            site_queries.update_site(conn, site["id"], {**values, "search_enabled": 1})
            self.assertIsNone(conn.execute("SELECT auto_paused_at FROM pt_sites WHERE id=?", (site["id"],)).fetchone()[0])

    async def test_resume_is_noop_for_manually_disabled_sites(self) -> None:
        site = self._site()
        with connect() as conn:
            conn.execute("UPDATE pt_sites SET search_enabled=0 WHERE id=?", (site["id"],))
            self.assertFalse(resume_after_check(conn, site["id"], site["name"]))
        self.assertEqual(self._row(site)["search_enabled"], 0)


class AutoBackupTests(IsolatedAppTestCase):
    def test_due_when_none_then_not_due_and_retention_per_kind(self) -> None:
        from app.database import initialize

        initialize()
        self.assertTrue(backup_service.auto_backup_due())
        name = backup_service.create_auto_backup()
        self.assertRegex(name, r"^autolist-auto-\d{8}-\d{6}\.db$")
        self.assertFalse(backup_service.auto_backup_due())
        listed = backup_service.list_backups()
        self.assertEqual([(row["name"], row["kind"]) for row in listed], [(name, "auto")])
        # 自动备份最多留 AUTO_KEEP 份，不挤占手动备份的名额。
        directory = Path(settings.data_dir) / "backups"
        for index in range(backup_service.AUTO_KEEP + 3):
            (directory / f"autolist-auto-20250101-0000{index:02d}.db").write_bytes(b"x")
        manual = backup_service.create_backup()
        self.assertEqual(len([p for p in directory.glob("autolist-auto-*.db")]), backup_service.AUTO_KEEP)
        self.assertTrue((directory / manual).exists())
        # 超过一天就又到期。
        past = time.time() - 25 * 3600
        for path in directory.glob("autolist-auto-*.db"):
            os.utime(path, (past, past))
        self.assertTrue(backup_service.auto_backup_due())


class PromotionTests(IsolatedAppTestCase):
    def test_nexusphp_reads_promotion_expiry_from_the_tooltip(self) -> None:
        base = "https://tracker-i.example/"
        page = (FIXTURES / "tracker-i.example.html").read_text(encoding="utf-8")
        rows = parse_page(page, profile_for(base), base, now=datetime(2026, 9, 28))
        timed = [row for row in rows if row.promotion_until]
        self.assertTrue(timed)
        self.assertEqual(timed[0].promotion_until, "2026-10-01 18:12:31")
        self.assertTrue(all(row.download_factor < 1 or row.upload_factor > 1 for row in timed))
        self.assertTrue(all(row.promotion_until is None for row in rows if row.download_factor == 1 and row.upload_factor == 1))

    def test_expired_promotion_is_detected_in_site_timezone(self) -> None:
        self.assertTrue(promotion_expired("2000-01-01 00:00:00"))
        self.assertFalse(promotion_expired("2999-01-01 00:00:00"))
        self.assertFalse(promotion_expired(None))
        self.assertFalse(promotion_expired("看不懂的文字"))

    def test_free_with_enough_seeders_leads_among_sites_and_expired_free_is_dropped(self) -> None:
        from app.services.candidates import present_candidates

        def row(candidate_id: str, site: str, seeders: int, factor: float, until: str | None) -> dict:
            return {
                "id": candidate_id, "playlist_item_id": 1, "rank_no": 1, "title": "Movie.2020.2160p.BluRay.x265-FRDS", "site_name": site,
                "size": 20 * 1024**3, "seeders": seeders, "resolution": "2160p", "codec": "x265", "group_name": "FRDS", "ranking": 1,
                "eligibility": "eligible", "metadata_json": json.dumps({"volume_factor": factor, "labels": ["FREE"] if factor == 0 else [], "promotion_until": until}),
                "score": 1, "score_breakdown": "[]", "recommendation": "preferred", "recommendation_reason": "", "detail_url": None,
                "library_state": "not_found", "resource_key": "k", "in_selection": 0, "task_id": 1,
            }

        sites = [{"name": "A", "priority": 100, "icon_url": ""}, {"name": "B", "priority": 100, "icon_url": ""}]
        # B 免费且做种不少于一半：排在做种略多的 A 前面。
        (candidate,) = present_candidates([row("a", "A", 20, 1.0, None), row("b", "B", 15, 0.0, "2999-01-01 00:00:00")], sites)
        self.assertEqual(candidate["site_name"], "B")
        self.assertEqual(candidate["free_until"], "2999-01-01 00:00:00")
        # B 免费但只有 1 人做种：不抢位。
        (candidate,) = present_candidates([row("a", "A", 20, 1.0, None), row("b", "B", 1, 0.0, None)], sites)
        self.assertEqual(candidate["site_name"], "A")
        # 免费已到期：不再算免费。
        (candidate,) = present_candidates([row("b", "B", 15, 0.0, "2000-01-01 00:00:00")], sites)
        self.assertFalse(candidate["is_free"])
        self.assertIsNone(candidate["free_until"])

    def test_free_ranks_ahead_of_full_price_only_with_enough_seeders(self) -> None:
        from app.database import config_values, initialize
        from app.services.recognition import analyze_candidate

        initialize()
        config = config_values()
        title = "Movie.2020.2160p.BluRay.x265-FRDS"

        def rank(seeders: int, volume: float) -> int:
            return analyze_candidate(title, 0, config, {"seeders": seeders, "volume_factor": volume})["ranking"]

        self.assertLess(rank(5, 0.0), rank(50, 1.0))   # 免费 5 人做种，排在原价 50 人做种之前
        self.assertGreater(rank(2, 0.0), rank(50, 1.0))  # 免费但只有 2 人做种：不享受优先


class SlowSiteSupplementTests(SeededPlaylistTestCase):
    async def test_slow_sites_are_searched_only_when_others_do_not_find_enough(self) -> None:
        with connect() as conn:
            item_id = to_int(conn.execute("SELECT id FROM playlist_items WHERE library_state!='in_library' ORDER BY id LIMIT 1").fetchone()[0])
            fast, slow = (to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at) VALUES(?,?,?,1,1,?)",
                (name, "nexusphp", f"https://{name}.example", utc_now()),
            ).lastrowid) for name in ("fast", "slow"))
            old_task = to_int(conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,1,1,'completed',1,?,?)",
                (self.playlist_id, utc_now(), utc_now()),
            ).lastrowid)
            for _ in range(6):  # 慢站点最近 6 次成功搜索平均 12 秒（每次 1 个检索词）
                conn.execute(
                    """INSERT INTO search_attempts(task_id,playlist_item_id,site_id,site_name,status,result_count,duration_ms,query_count,created_at,finished_at)
                       VALUES(?,?,?,?,'success',3,12000,1,?,?)""", (old_task, item_id, slow, "slow", utc_now(), utc_now()),
                )
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,site_ids_json,item_ids_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "queued", 1, json.dumps([fast, slow]), json.dumps([item_id]), utc_now(), utc_now()),
            ).lastrowid)
        with connect() as conn:
            self.assertEqual(site_queries.slow_site_ids(conn, 6000), {slow})
        searched: list[str] = []

        def fake_factory(count: int):
            async def fake_search_site(_task_id, item, site, *_a, **_k):
                searched.append(site["name"])
                torrents = [
                    {"title": f"Movie.{2000 + index}.2160p.BluRay.x265-FRDS", "size": (20 + index) * 1024**3, "seeders": 10, "site_name": site["name"],
                     "_site_id": site["id"], "imdbid": "tt1"}
                    for index in range(count if site["name"] == "fast" else 0)
                ]
                return site, torrents, None, 1, None
            return fake_search_site

        media = {"id": 91, "title": "Movie", "original_title": "Movie", "release_date": "2000-01-01", "imdb_id": "tt1"}
        # 已经够用（快站点没找到足够候选 → 才轮到慢站点；这里先验证“找不到”时会补搜）。
        with patch("app.services.search.recognize_item", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.search_one_site", new=fake_factory(0)):
            await run_search(task_id)
        self.assertEqual(searched, ["fast", "slow"])  # 候选不足：慢站点补搜
        # 快站点已经找到 3 个以上不同发布：慢站点不再请求。
        searched.clear()
        with connect() as conn:
            task3 = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,site_ids_json,item_ids_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "queued", 1, json.dumps([fast, slow]), json.dumps([item_id]), utc_now(), utc_now()),
            ).lastrowid)
        with patch("app.services.search.recognize_item", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.search_one_site", new=fake_factory(4)):
            await run_search(task3)
        self.assertEqual(searched, ["fast"])
        # 关闭开关后，慢站点与其他站点一起搜（顺序不限）。
        searched.clear()
        settings.search_slow_as_supplement = False
        with connect() as conn:
            task2 = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,site_ids_json,item_ids_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "queued", 1, json.dumps([fast, slow]), json.dumps([item_id]), utc_now(), utc_now()),
            ).lastrowid)
        with patch("app.services.search.recognize_item", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.search_one_site", new=fake_factory(0)):
            await run_search(task2)
        self.assertEqual(sorted(searched), ["fast", "slow"])


class SyncEventResilienceTests(IsolatedAppTestCase):
    async def test_handler_failure_returns_accepted_false_not_500(self) -> None:
        from app.services import sync_events

        request = MagicMock(spec=Request)
        request.client.host = "127.0.0.1"
        request.query_params = {"token": "t"}
        request.body = AsyncMock(return_value=b"{}")
        with patch.object(sync_events, "authorized", return_value=True), \
             patch.object(sync_events, "parse", side_effect=RuntimeError("boom")):
            self.assertEqual(await sync_routes.sync_event("transmission", request), {"accepted": False})
        request.body = AsyncMock(side_effect=RuntimeError("body"))
        with patch.object(sync_events, "authorized", return_value=True):
            self.assertEqual(await sync_routes.sync_event("transmission", request), {"accepted": False})

    def test_long_tracebacks_keep_head_and_tail(self) -> None:
        text = "Traceback head\n" + "x" * 5000 + "\nValueError: the real cause"
        short = _traceback_text(text)
        self.assertTrue(short.startswith("Traceback head"))
        self.assertTrue(short.endswith("ValueError: the real cause"))
        self.assertLess(len(short), TRACEBACK_HEAD + TRACEBACK_TAIL + 100)
        self.assertEqual(_traceback_text("short"), "short")
