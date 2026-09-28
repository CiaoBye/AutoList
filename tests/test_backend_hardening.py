import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.compat import main  # noqa: E402 (审计 2-12：测试兼容层) # type: ignore[import-not-found]
from tests.support import task_candidates
from app.config import (
    ACCESS_TOKEN_MIN_LENGTH,
    access_token_is_strong,
    access_token_strength,
    access_token_strength_enforced,
    access_token_validation_error,
    settings,
)
from app.database import connect, initialize
from app.schemas import ImportPayload, TaskPayload
from app.services import automation
from app.util import safe_detail_url, safe_request, to_int, validate_outbound_url


class BackendHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous_data_dir = settings.data_dir
        self.previous_token = os.environ.get("AUTOLIST_ACCESS_TOKEN")
        self.previous_strict = os.environ.get("AUTOLIST_REQUIRE_STRONG_TOKEN")
        self.previous_tr = settings.tr_base_url
        self.previous_emby = (settings.emby_base_url, settings.emby_api_key)
        settings.data_dir = self.temp.name
        settings.tr_base_url = ""
        settings.emby_base_url = ""
        settings.emby_api_key = ""
        os.environ.pop("AUTOLIST_ACCESS_TOKEN", None)
        os.environ.pop("AUTOLIST_REQUIRE_STRONG_TOKEN", None)
        main.raw_candidates.clear()
        initialize()

    async def asyncTearDown(self) -> None:
        main.raw_candidates.clear()
        from app.services import search as search_module
        search_module._transmission_snapshot_cache.clear()
        settings.data_dir = self.previous_data_dir
        settings.tr_base_url = self.previous_tr
        settings.emby_base_url, settings.emby_api_key = self.previous_emby
        if self.previous_token is None:
            os.environ.pop("AUTOLIST_ACCESS_TOKEN", None)
        else:
            os.environ["AUTOLIST_ACCESS_TOKEN"] = self.previous_token
        if self.previous_strict is None:
            os.environ.pop("AUTOLIST_REQUIRE_STRONG_TOKEN", None)
        else:
            os.environ["AUTOLIST_REQUIRE_STRONG_TOKEN"] = self.previous_strict
        self.temp.cleanup()

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
                result = await main.TMDBClient().check()
            self.assertEqual(result, {"ok": True, "configured": True})
            call = request.await_args
            assert call is not None
            self.assertEqual(call.args[2], "configuration")
            self.assertEqual(str(call.args[0].base_url), "https://api.themoviedb.org/3/")
        finally:
            settings.tmdb_api_key = previous_key
            settings.outbound_proxy_url = previous_proxy
            settings.tmdb_proxy_enabled = previous_tmdb_proxy

    def _seed_cart(self, candidate_id: str = "hardening-candidate") -> int:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("Hardening", 1, main.utc_now()),
            ).lastrowid)
            item_id = to_int(conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,original_title,year,chinese_title,library_state)
                   VALUES(?,?,?,?,?,?)""",
                (playlist_id, 1, "Hardening Movie", 2020, "测试电影", "not_found"),
            ).lastrowid)
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid)
            conn.execute(
                """INSERT INTO candidates(
                     id,task_id,playlist_item_id,candidate_index,title,site_name,resource_key,ranking,
                     eligibility,metadata_json,detail_url,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate_id, task_id, item_id, 0, "Hardening.Movie.1080p", "TestSite",
                    "hardening:1", 1, "eligible", "{}",
                    "https://tracker.example/details.php?id=42&passkey=database-secret&token=legacy-secret",
                    main.utc_now(),
                ),
            )
            conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", (candidate_id, main.utc_now()))
        main.raw_candidates[candidate_id] = {
            "media": {"id": 42, "title": "Hardening Movie", "type": "电影"},
            "torrent": {"title": "Hardening.Movie.1080p"},
        }
        return task_id

    def test_access_token_strength_is_visible_without_leaking_token(self) -> None:
        weak = "unit-test-token"
        strong = "A9b8C7d6" * (ACCESS_TOKEN_MIN_LENGTH // 8)
        self.assertIsNotNone(access_token_validation_error(weak))
        self.assertFalse(access_token_is_strong(weak))
        self.assertIsNone(access_token_validation_error(strong))
        self.assertTrue(access_token_is_strong(strong))
        os.environ["AUTOLIST_ACCESS_TOKEN"] = weak
        self.assertEqual(access_token_strength(), "weak")
        public = settings.public_values()
        self.assertEqual(public["access_token_strength"], "weak")
        self.assertNotIn(weak, json.dumps(public, ensure_ascii=False))

    def test_strict_token_mode_blocks_weak_token_but_health_remains_diagnostic(self) -> None:
        os.environ["AUTOLIST_ACCESS_TOKEN"] = "short-token"
        os.environ["AUTOLIST_REQUIRE_STRONG_TOKEN"] = "true"
        with TestClient(main.app) as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["access_token_strength"], "weak")
            blocked = client.get("/api/settings", headers={"X-AutoList-Token": "short-token"})
        self.assertEqual(blocked.status_code, 503)
        # 审计 3-12：503 只返回统一提示，不泄露鉴权配置明细。
        self.assertEqual(blocked.json()["detail"], "服务端访问令牌强度不足，请更换至少 32 个字符的随机令牌")
        self.assertNotIn("code", blocked.json())
        self.assertNotIn("configured", blocked.json())
        self.assertNotIn("validation", blocked.json())

    def test_token_strength_is_enforced_by_default_when_token_is_configured(self) -> None:
        os.environ["AUTOLIST_ACCESS_TOKEN"] = "short-token"
        os.environ.pop("AUTOLIST_REQUIRE_STRONG_TOKEN", None)
        self.assertTrue(access_token_strength_enforced())
        with TestClient(main.app) as client:
            blocked = client.get("/api/settings", headers={"X-AutoList-Token": "short-token"})
        self.assertEqual(blocked.status_code, 503)

    async def test_safe_request_validates_redirects_and_strips_cross_origin_credentials(self) -> None:
        seen: list[tuple[str, str | None, str | None, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), request.headers.get("authorization"), request.headers.get("cookie"), request.headers.get("x-api-key")))
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "https://other.example/final"})
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer secret", "Cookie": "uid=secret", "X-API-Key": "api-secret"},
            follow_redirects=False,
        ) as client:
            response = await safe_request(
                client, "GET", "https://source.example/start",
                headers={"Authorization": "Bearer secret", "Cookie": "uid=secret", "X-API-Key": "api-secret"},
                allow_private=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][1], "Bearer secret")
        self.assertEqual(seen[0][2], "uid=secret")
        self.assertEqual(seen[0][3], "api-secret")
        self.assertIsNone(seen[1][1])
        self.assertIsNone(seen[1][2])
        self.assertIsNone(seen[1][3])

    async def test_safe_request_rejects_sensitive_cross_origin_redirect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://other.example/final?token=leak"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            with self.assertRaisesRegex(RuntimeError, "敏感查询参数"):
                await safe_request(client, "GET", "https://source.example/start", allow_private=True)

    def test_outbound_validator_and_detail_url_keep_safe_semantics(self) -> None:
        with self.assertRaisesRegex(ValueError, "内网"):
            validate_outbound_url("http://127.0.0.1:8080/api", allow_private=False)
        self.assertEqual(validate_outbound_url("http://127.0.0.1:8080/api", allow_private=True), "http://127.0.0.1:8080/api")
        safe = safe_detail_url("https://tracker.example/details.php?id=42&passkey=secret&token=secret#private")
        self.assertEqual(safe, "https://tracker.example/details.php?id=42")
        self.assertIsNone(safe_detail_url("magnet:?xt=urn:btih:private"))

    async def test_candidate_and_cart_apis_clean_legacy_detail_url(self) -> None:
        task_id = self._seed_cart()
        candidates = task_candidates(task_id)
        self.assertEqual(candidates[0]["detail_url"], "https://tracker.example/details.php?id=42")
        self.assertEqual(candidates[0]["site_options"][0]["detail_url"], "https://tracker.example/details.php?id=42")
        cart = await main.cart()
        self.assertEqual(cart[0]["detail_url"], "https://tracker.example/details.php?id=42")

    async def test_transmission_failure_blocks_cart_submission_and_keeps_cart(self) -> None:
        self._seed_cart("transmission-unknown")
        settings.tr_base_url = "http://transmission.example:9091"
        with patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(side_effect=RuntimeError("offline"))), \
             patch.object(main.MoviePilotClient, "download", new=AsyncMock()) as download:
            with self.assertRaises(HTTPException) as raised:
                await main.download_cart()
        self.assertEqual(raised.exception.status_code, 503)
        download.assert_not_awaited()
        self.assertEqual([item["id"] for item in await main.cart()], ["transmission-unknown"])

    async def test_emby_unknown_blocks_candidate_without_treating_empty_match_as_failure(self) -> None:
        self._seed_cart("emby-unknown")
        with patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])), \
             patch("app.api.cart.library_details", new=AsyncMock(return_value=("unknown", None, None))), \
             patch.object(main.MoviePilotClient, "download", new=AsyncMock()) as download:
            result = await main.download_cart()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(result["blocked_unknown"][0]["candidate_id"], "emby-unknown")
        download.assert_not_awaited()
        self.assertEqual([item["id"] for item in await main.cart()], ["emby-unknown"])

    async def test_search_queue_distinguishes_unknown_from_known_empty_transmission(self) -> None:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("Queue", 1, main.utc_now()),
            ).lastrowid)
            conn.execute(
                "INSERT INTO playlist_items(playlist_id,rank_no,original_title,year) VALUES(?,?,?,?)",
                (playlist_id, 1, "Queue Movie", 2020),
            )
        settings.tr_base_url = "http://transmission.example:9091"
        with patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(side_effect=RuntimeError("offline"))):
            unknown = await main.searchable_playlist_items(playlist_id, limit=10)
        self.assertEqual(unknown["download_state"], "unknown")
        self.assertEqual(unknown["items"], [])
        settings.tr_base_url = ""
        from app.services import search as search_module
        search_module._transmission_snapshot_cache.clear()
        with patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])):
            empty = await main.searchable_playlist_items(playlist_id, limit=10)
        self.assertEqual(empty["download_state"], "known_empty")
        self.assertEqual(len(empty["items"]), 1)

    def test_search_and_import_payloads_have_server_limits(self) -> None:
        with self.assertRaises(ValidationError):
            TaskPayload(playlist_id=1, scope="range", range_start=1, range_end=2001)
        with self.assertRaises(ValidationError):
            ImportPayload(json_data={"films": [{"title": "x", "description": "x" * (10 * 1024 * 1024)}]})

    async def test_playlist_sync_lock_rejects_same_playlist_overlap(self) -> None:
        playlist_id = 987654
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_sync(_playlist_id: int, _trigger: str) -> dict[str, object]:
            entered.set()
            await release.wait()
            return {"id": _playlist_id}

        with patch("app.services.automation._sync_playlist_incremental", new=blocked_sync):
            first = asyncio.create_task(automation.sync_playlist_incremental(playlist_id))
            await entered.wait()
            with self.assertRaises(HTTPException) as raised:
                await automation.sync_playlist_incremental(playlist_id)
            self.assertEqual(raised.exception.status_code, 409)
            release.set()
            self.assertEqual((await first)["id"], playlist_id)


if __name__ == "__main__":
    unittest.main()
