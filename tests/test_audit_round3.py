"""1.44 审计修复回归：适配器迁移、CookieCloud KEY、识别与入库联动、站点同步与限速、界面契约。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from app.clients import MTeamClient
from app.config import settings
from app.database import connect, initialize
from app.schemas import CookieCloudUploadPayload, ImportPayload, RuntimeSettingsPayload
from app.util import to_int, utc_now
from tests.support import IsolatedAppTestCase


def _insert_site(**values: object) -> int:
    row = {"name": "站点", "adapter": "nexusphp", "base_url": "https://pt.example", "created_at": utc_now()}
    row.update(values)
    columns = ",".join(row)
    placeholders = ",".join("?" for _ in row)
    with connect() as conn:
        return to_int(conn.execute(
            f"INSERT INTO pt_sites({columns}) VALUES({placeholders})", tuple(row.values()),  # nosec B608
        ).lastrowid)


def _adapter(site_id: int) -> str:
    with connect() as conn:
        return str(conn.execute("SELECT adapter FROM pt_sites WHERE id=?", (site_id,)).fetchone()["adapter"])


class SiteAdapterMigrationTests(IsolatedAppTestCase):
    async def test_rss_only_site_survives_restarts(self) -> None:
        site_id = _insert_site(name="纯 RSS", adapter="rss", base_url="https://example.org", rss_url="https://example.org/feed.xml")
        initialize()
        initialize()
        self.assertEqual(_adapter(site_id), "rss")

    async def test_upgrade_recomputes_adapters_once(self) -> None:
        wrong_rss = _insert_site(name="有 Cookie 的 RSS", adapter="rss", base_url="https://a.example", rss_url="https://a.example/rss", cookie="uid=1")
        broken_rss = _insert_site(name="被误改的 RSS", adapter="nexusphp", base_url="https://b.example", rss_url="https://b.example/rss")
        plain = _insert_site(name="普通站点", adapter="nexusphp", base_url="https://c.example", cookie="uid=2")
        mteam = _insert_site(name="馒头", adapter="mteam", base_url="https://kp.m-team.cc", rss_url="https://kp.m-team.cc/rss")
        with connect() as conn:
            # 模拟 1.43（schema v3）数据库升级：只有 v3→v4 迁移会重算适配器。
            conn.execute("PRAGMA user_version=3")
        initialize()
        self.assertEqual(_adapter(wrong_rss), "nexusphp")
        self.assertEqual(_adapter(broken_rss), "rss")
        self.assertEqual(_adapter(plain), "nexusphp")
        self.assertEqual(_adapter(mteam), "mteam")
        # 迁移只执行一次：之后用户手动选择的状态不会在重启时被再次改写。
        with connect() as conn:
            conn.execute("UPDATE pt_sites SET adapter='nexusphp' WHERE id=?", (broken_rss,))
        initialize()
        self.assertEqual(_adapter(broken_rss), "nexusphp")

    async def test_identical_cookie_is_not_rewritten(self) -> None:
        from app.services.sites import apply_cookie_groups

        checked_at = utc_now()
        site_id = _insert_site(
            name="Cookie 未变化", base_url="https://pt.same.example", cookie="session=1",
            account_stats_checked_at=checked_at,
        )
        result = apply_cookie_groups({"same.example": "session=1"})
        self.assertEqual((result["updated"], result["unchanged"]), ([], ["Cookie 未变化"]))
        with connect() as conn:
            row = conn.execute("SELECT account_stats_checked_at FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        # 账户统计缓存不能因为每小时的同步被重置。
        self.assertEqual(row["account_stats_checked_at"], checked_at)

    async def test_cookie_from_cookiecloud_switches_rss_site_to_nexusphp(self) -> None:
        from app.services.sites import apply_cookie_groups

        site_id = _insert_site(name="RSS 转页面", adapter="rss", base_url="https://pt.rss.example", rss_url="https://pt.rss.example/rss")
        apply_cookie_groups({"rss.example": "session=1"})
        self.assertEqual(_adapter(site_id), "nexusphp")


class CookieCloudKeyTests(IsolatedAppTestCase):
    async def test_settings_and_upload_accept_the_same_key_format(self) -> None:
        RuntimeSettingsPayload(cookiecloud_key="abcde")
        CookieCloudUploadPayload(uuid="abcde", encrypted="x" * 16)
        for invalid in ("abcd", "bad/key"):
            with self.subTest(key=invalid):
                with self.assertRaises(ValidationError):
                    RuntimeSettingsPayload(cookiecloud_key=invalid)
                with self.assertRaises(ValidationError):
                    CookieCloudUploadPayload(uuid=invalid, encrypted="x" * 16)

    async def test_status_endpoint_never_returns_url_secrets(self) -> None:
        from app.api.system import cookiecloud_status

        settings.cookiecloud_url = "http://cc.example:8088/cookiecloud?token=SECRET_VALUE"
        status = await cookiecloud_status()
        self.assertNotIn("SECRET_VALUE", status["url"])
        self.assertTrue(status["url_configured"])


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


class SiteSyncAndStatsTests(IsolatedAppTestCase):
    async def test_moviepilot_sync_preserves_local_choices_and_skips_collisions(self) -> None:
        from app.services.sites import sync_sites_from_moviepilot

        local_id = _insert_site(
            name="本地天空", base_url="https://hdsky.me", priority=7, enabled=1, search_enabled=0,
            user_agent="My UA", cookie="old=1",
        )
        renamed_domain = _insert_site(name="彩虹岛", base_url="https://old-domain.example", priority=9)
        remote = [
            {"name": "天空", "url": "https://hdsky.me/", "cookie": "new=1", "pri": 1, "ua": "MP UA", "is_active": True},
            {"name": "彩虹岛", "url": "https://chdbits.co/", "cookie": "c=1", "pri": 2},
            {"name": "坏地址", "url": "ftp://bad.example/"},
            {"name": "新站", "url": "https://new.example/", "cookie": "n=1", "apikey": "k", "pri": 5},
            {"name": "新站镜像", "url": "https://new.example/", "cookie": "n=2"},
        ]
        with patch("app.services.sites.fetch_moviepilot_sites", new=AsyncMock(return_value=remote)):
            result = await sync_sites_from_moviepilot()
        with connect() as conn:
            local = conn.execute("SELECT * FROM pt_sites WHERE id=?", (local_id,)).fetchone()
            new_sites = conn.execute("SELECT * FROM pt_sites WHERE base_url='https://new.example'").fetchall()
            moved = conn.execute("SELECT * FROM pt_sites WHERE id=?", (renamed_domain,)).fetchone()
        self.assertEqual(
            (local["name"], local["priority"], local["search_enabled"], local["user_agent"], local["cookie"]),
            ("本地天空", 7, 0, "My UA", "new=1"),
        )
        self.assertEqual(len(new_sites), 1)
        self.assertEqual(new_sites[0]["api_key"], "k")
        # 同名站点视为更换了域名：更新地址，但保留本地优先级。
        self.assertEqual((moved["base_url"], moved["priority"]), ("https://chdbits.co", 9))
        self.assertEqual(result["skipped"], ["坏地址（地址无效）"])

    async def test_account_stats_without_credentials_skip_network(self) -> None:
        from app.services.sites import refresh_site_account_stats

        site_id = _insert_site(name="无 Cookie", cookie="")
        with connect() as conn:
            site = dict(conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone())
        with patch("app.services.sites.NexusPHPClient.account_stats", new=AsyncMock()) as stats:
            result = await refresh_site_account_stats(site)
        stats.assert_not_awaited()
        self.assertFalse(result["ok"])
        self.assertIn("未配置 Cookie", result["error"])

    async def test_site_errors_are_translated(self) -> None:
        from app.services.sites import friendly_site_error

        request = httpx.Request("GET", "https://pt.example/userdetails.php?passkey=abc")
        error = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        message = friendly_site_error(error)
        self.assertIn("HTTP 404", message)
        self.assertNotIn("developer.mozilla.org", message)
        self.assertNotIn("passkey", message)


class RateLimitAndDiscountTests(IsolatedAppTestCase):
    async def test_limit_count_allows_burst_then_waits_for_window(self) -> None:
        from app.services.search import wait_for_site_rate_limit

        site = {"id": 4242, "limit_interval": 0.2, "limit_count": 2}
        started = time.monotonic()
        await asyncio.gather(*(wait_for_site_rate_limit(site) for _ in range(3)))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 0.6)

    async def test_mteam_double_upload_discounts(self) -> None:
        self.assertEqual(MTeamClient._discount_factor("_2X_FREE"), 0.0)
        self.assertEqual(MTeamClient._discount_factor("_2X_PERCENT_50"), 0.5)
        self.assertEqual(MTeamClient._discount_factor("_2X"), 1.0)
        row = MTeamClient()._parse_row(
            {"id": 1, "name": "Movie", "status": {"discount": "_2X_FREE"}},
            {"name": "M-Team", "base_url": "https://kp.m-team.cc", "api_key": "k"},
        )
        self.assertEqual(row["volume_factor"], 0.0)
        self.assertEqual(row["labels"][:2], ["2X", "FREE"])


class StaticAssetCacheTests(IsolatedAppTestCase):
    async def test_static_modules_are_revalidated(self) -> None:
        from app.main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/assets/logo.svg")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("cache-control"), "no-cache")
            etag = response.headers.get("etag")
            self.assertTrue(etag)
            revalidated = await client.get("/assets/logo.svg", headers={"If-None-Match": etag})
            self.assertEqual(revalidated.status_code, 304)


class InterfaceContractTests(unittest.TestCase):
    """新界面（frontend/ → app/static/ui）的静态契约：旧界面已移除，主题只改 token 不改组件。"""

    root = Path(__file__).resolve().parents[1]

    def test_legacy_interface_is_removed(self) -> None:
        static = self.root / "app" / "static"
        for name in ("index.html", "app.js", "style.css", "theme.css", "js/core.js", "js/site-map.js", "js/theme-init.js"):
            self.assertFalse((static / name).exists(), name)
        # 新设置页仍引用的品牌与服务图标保留。
        for name in ("logo.svg", "favicon.svg", "autolist-icon.png"):
            self.assertTrue((static / name).exists(), name)

    def test_themes_only_switch_tokens(self) -> None:
        styles = self.root / "frontend" / "src"
        self.assertNotIn("[data-theme", (styles / "app.css").read_text(encoding="utf-8"))
        self.assertIn('[data-theme="cinema"]', (styles / "tokens.css").read_text(encoding="utf-8"))

    def test_format_size_reaches_terabytes(self) -> None:
        script = (
            "import { formatSize } from './frontend/src/format.ts';"
            "const G = 1024 ** 3;"
            "console.log(JSON.stringify([0, 5 * G, 48294 * G, 250 * 1024 * G, 3 * 1024 ** 2].map(formatSize)));"
        )
        output = subprocess.check_output(
            ["node", "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", script],
            cwd=self.root, text=True,
        )
        self.assertEqual(json.loads(output), ["—", "5.0 GB", "47.2 TB", "250 TB", "3 MB"])
if __name__ == "__main__":
    unittest.main()
