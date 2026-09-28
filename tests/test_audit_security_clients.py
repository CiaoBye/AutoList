"""Regression coverage for settings-adjacent client and credential handling."""

import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app.api.sites import add_site, sites, update_site
from app.clients import AIRecognitionClient
from app.config import settings
from app.database import connect
from app.schemas import SitePayload
from app.security import safe_error
from tests.support import IsolatedAppTestCase


class DiagnosticRedactionTests(unittest.TestCase):
    def test_all_url_query_credentials_are_redacted(self):
        for query in (
            "key=AUDIT_FIRST&token=AUDIT_SECOND",
            "key=AUDIT_FIRST&key=AUDIT_SECOND",
            "%6bey=AUDIT_FIRST&api_key=AUDIT_SECOND",
        ):
            with self.subTest(query=query):
                text = safe_error(RuntimeError(f"Failed https://example.org/rss?{query}&id=7"))
                self.assertNotIn("AUDIT_FIRST", text)
                self.assertNotIn("AUDIT_SECOND", text)
                self.assertIn("id=7", text)


class ClientSettingsAuditTests(IsolatedAppTestCase):
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

    async def test_private_rss_can_be_saved_replaced_and_remains_hidden(self):
        first = "https://example.org/rss?passkey=audit-first"
        created = await add_site(SitePayload(name="Audit RSS", base_url="https://example.org", rss_url=first))
        second = "https://example.org/rss?key=audit-second"
        await update_site(created["id"], SitePayload(name="Audit RSS", base_url="https://example.org", rss_url=second))
        with connect() as conn:
            row = conn.execute("SELECT rss_url FROM pt_sites WHERE id=?", (created["id"],)).fetchone()
        self.assertEqual(row["rss_url"], second)
        public = (await sites())[0]
        self.assertEqual(public["rss_url"], "")
        self.assertTrue(public["rss_url_configured"])

    async def test_rss_retains_scheme_userinfo_and_private_host_restrictions(self):
        for url in ("file:///tmp/feed", "https://user:password@example.org/rss", "http://127.0.0.1/rss?key=audit"):
            with self.subTest(url=url), patch.dict(os.environ, {"AUTOLIST_ACCESS_TOKEN": "audit-enabled", "AUTOLIST_ALLOW_PRIVATE_HOSTS": ""}):
                with self.assertRaises(HTTPException) as caught:
                    await add_site(SitePayload(name="Invalid RSS", base_url="https://8.8.8.8", rss_url=url))
                self.assertEqual(caught.exception.status_code, 422)

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

    def test_token_failure_rate_limit_per_client_ip(self):
        from app.main import TOKEN_FAILURE_LIMIT, _token_failure_by_ip, _token_failure_rate_limited

        _token_failure_by_ip.clear()
        bad_ip = "198.51.100.1"
        good_ip = "203.0.113.2"

        # 连续失败达到阈值前允许尝试
        for _ in range(TOKEN_FAILURE_LIMIT):
            self.assertFalse(_token_failure_rate_limited(bad_ip))

        # 达到阈值后该 IP 被限速
        self.assertTrue(_token_failure_rate_limited(bad_ip))

        # 另一合法 IP 不受影响
        self.assertFalse(_token_failure_rate_limited(good_ip))

    async def test_search_one_site_respects_limit_interval(self):
        import asyncio
        import time
        from unittest.mock import AsyncMock
        from app.services.search import _site_request_times, search_one_site

        _site_request_times.clear()
        site = {"id": 999, "name": "RateLimitedSite", "adapter": "nexusphp", "limit_interval": 0.05}
        client = AsyncMock()
        client.search = AsyncMock(return_value=[])
        clients = {"nexusphp": client}
        semaphore = asyncio.Semaphore(1)
        queries = [("Query1", None, "Q1"), ("Query2", None, "Q2")]
        item = {"id": 1, "rank_no": 1, "original_title": "Test Movie"}

        start = time.monotonic()
        with patch("app.services.search.record_search_attempt"):
            await search_one_site(1, item, site, clients, semaphore, queries)
        elapsed = time.monotonic() - start

        self.assertEqual(client.search.await_count, 2)
        # 两次检索词之间有 limit_interval=0.05s 延时
        self.assertGreaterEqual(elapsed, 0.04)

    def test_log_formatter_sanitizes_traceback_and_event(self):
        import logging
        import json
        from app.logs import JsonLineFormatter

        formatter = JsonLineFormatter()
        record = logging.LogRecord(
            name="autolist",
            level=logging.ERROR,
            pathname=__file__,
            lineno=10,
            msg="Request failed for https://site.com/download.php?passkey=abcdef1234567890",
            args=(),
            exc_info=None,
        )
        try:
            raise ValueError("Secret token: Bearer super_secret_access_token_12345")
        except ValueError:
            import sys
            record.exc_info = sys.exc_info()

        formatted = formatter.format(record)
        payload = json.loads(formatted)
        self.assertNotIn("abcdef1234567890", payload["event"])
        self.assertIn("passkey=***", payload["event"])
        self.assertIn("error", payload)
        self.assertNotIn("super_secret_access_token_12345", payload["error"])
        self.assertIn("***", payload["error"])

    def test_active_history_matches_indexed_lookup(self):
        from app.services.history import _active_history_matches

        histories = [
            {"id": 1, "success": 1, "submission_hash": "aabbcc11", "torrent_name": "Movie.A.1080p"},
            {"id": 2, "success": 1, "submission_hash": "ddeeff22", "torrent_name": "Movie.B.1080p"},
            {"id": 3, "success": 1, "submission_hash": "ddeeff22", "torrent_name": "Movie.B.Dup"},
            {"id": 4, "success": 0, "submission_hash": "11223344", "torrent_name": "Movie.Failed"},
        ]
        torrents = [
            {"hashString": "aabbcc11", "name": "Movie.A.1080p", "status": 4},  # downloading
            {"hashString": "ddeeff22", "name": "Movie.B.1080p", "status": 4},  # ambiguous
            {"hashString": "11223344", "name": "Movie.Failed", "status": 4},   # not eligible
        ]
        matched, ambiguous = _active_history_matches(histories, torrents)
        self.assertIn(1, matched)
        self.assertNotIn(1, ambiguous)
        self.assertIn(2, ambiguous)
        self.assertIn(3, ambiguous)
        self.assertNotIn(4, matched)

    def test_unhandled_exception_handler(self):
        from starlette.testclient import TestClient
        from app.main import app

        @app.get("/test-unhandled-error")
        async def _faulty_endpoint():
            raise RuntimeError("Database connection secret_key=xyz exploded")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test-unhandled-error")
        self.assertEqual(response.status_code, 500)
        data = response.json()
        self.assertEqual(data.get("detail"), "系统内部错误，请稍后重试")
        self.assertNotIn("secret_key", response.text)
        self.assertNotIn("exploded", response.text)

    def test_cookiecloud_cors_and_route_aliases(self):
        from starlette.testclient import TestClient
        from app.main import app

        client = TestClient(app, raise_server_exceptions=False)
        # 1. OPTIONS 预检请求返回 204 与 CORS / PNA 头
        for path in ("/cookiecloud/update", "/update", "/cookiecloud/get/abc", "/get/abc"):
            options_res = client.options(path)
            self.assertEqual(options_res.status_code, 204, f"OPTIONS failed on {path}")
            self.assertEqual(options_res.headers.get("access-control-allow-origin"), "*")
            self.assertEqual(options_res.headers.get("access-control-allow-private-network"), "true")

        # 2. 根路径 /update 与子路径 /cookiecloud/update 别名路由均可触达
        post_root = client.post("/update", json={})
        self.assertIn(post_root.status_code, (422, 429), f"Failed on /update: {post_root.status_code}")
        self.assertEqual(post_root.headers.get("access-control-allow-origin"), "*")

        post_sub = client.post("/cookiecloud/update", json={})
        self.assertIn(post_sub.status_code, (422, 429), f"Failed on /cookiecloud/update: {post_sub.status_code}")
        self.assertEqual(post_sub.headers.get("access-control-allow-origin"), "*")

    async def test_cookiecloud_operations_and_event_logging(self):
        import json
        from unittest.mock import patch
        from app.api import sites as site_routes
        from app.database import connect
        from app.logs import event_logger
        from app.util import to_int, utc_now

        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,user_agent,created_at) VALUES(?,?,?,?,?,?)",
                ("日志测试站点", "nexusphp", "https://test-log.org", "old=1", "TestUA", utc_now()),
            ).lastrowid)

        try:
            mock_payload = {"cookie_data": {".test-log.org": [{"domain": ".test-log.org", "name": "c_session", "value": "testlogcookieval"}]}}
            with patch("app.api.sites.stored_cookiecloud_payload", return_value=mock_payload):
                with patch.object(event_logger(), "info") as mock_info:
                    sync_res = await site_routes.sync_sites_from_cookiecloud()
                    self.assertTrue(sync_res["ok"])
                    mock_info.assert_called()
                    call_args = mock_info.call_args
                    self.assertEqual(call_args[0][0], "cookiecloud_sync")
                    self.assertIn("detail", call_args[1].get("extra", {}))

                with patch.object(event_logger(), "info") as mock_info:
                    refresh_res = await site_routes.refresh_site_cookie(site_id)
                    self.assertTrue(refresh_res["ok"])
                    mock_info.assert_called()
                    call_args = mock_info.call_args
                    self.assertEqual(call_args[0][0], "site_cookie_refreshed")
                    self.assertIn("detail", call_args[1].get("extra", {}))
        finally:
            with connect() as conn:
                conn.execute("DELETE FROM pt_sites WHERE id=?", (site_id,))

    async def test_cookiecloud_remote_sync_and_test_provider(self) -> None:
        from app.api import sites as site_routes
        from app.api import system as system_routes
        from app.config import settings

        orig_url = settings.cookiecloud_url
        orig_key = settings.cookiecloud_key
        orig_pass = settings.cookiecloud_password
        try:
            settings.cookiecloud_url = "http://192.168.31.100:3000/cookiecloud"
            settings.cookiecloud_key = "test-key"
            settings.cookiecloud_password = "test-pass"

            mock_payload = {
                "cookie_data": {
                    ".hdsky.me": [{"domain": ".hdsky.me", "name": "c_secure_uid", "value": "test_uid"}]
                }
            }
            with patch("app.api.sites.fetch_remote_cookiecloud", new_callable=AsyncMock) as mock_fetch, \
                 patch("app.services.cookiecloud_store.fetch_remote_cookiecloud", new_callable=AsyncMock) as mock_test_fetch:
                mock_fetch.return_value = mock_payload
                mock_test_fetch.return_value = mock_payload
                res = await site_routes.sync_sites_from_cookiecloud()
                self.assertIn("message", res)
                mock_fetch.assert_awaited_once_with(
                    "http://192.168.31.100:3000/cookiecloud", "test-key", "test-pass"
                )

                test_res = await system_routes.test_runtime_settings("cookiecloud")
                self.assertIn("cookiecloud", test_res)
                self.assertTrue(test_res["cookiecloud"]["ok"])
        finally:
            settings.cookiecloud_url = orig_url
            settings.cookiecloud_key = orig_key
            settings.cookiecloud_password = orig_pass

    async def test_sync_sites_from_moviepilot_and_cookiecloud_auto_import(self) -> None:
        from app.api import sites as site_routes

        mock_mp_sites = [
            {"id": 1, "name": "天空", "url": "https://hdsky.me/", "cookie": "c_uid=1", "pri": 1, "is_active": True},
            {"id": 2, "name": "彩虹岛", "url": "https://chdbits.co/", "cookie": "", "pri": 2, "is_active": True},
            {"id": 3, "name": "彩虹岛", "url": "https://ptchdbits.co/", "cookie": "ptchd=1", "pri": 3, "is_active": True},
        ]
        with patch("app.services.sites.fetch_moviepilot_sites", new_callable=AsyncMock) as mock_sites:
            mock_sites.return_value = mock_mp_sites
            res = await site_routes.sync_sites_from_mp()
            self.assertTrue(res["ok"])
            self.assertEqual(res["total"], 3)
            with connect() as conn:
                rows = conn.execute("SELECT name, base_url, cookie FROM pt_sites ORDER BY id").fetchall()
                names = [r["name"] for r in rows]
                self.assertIn("天空", names)
                # Disambiguation happened for duplicates
                self.assertTrue(any("chdbits.co" in n for n in names))
                self.assertTrue(any("ptchdbits.co" in n for n in names))
                conn.execute("DELETE FROM pt_sites")
