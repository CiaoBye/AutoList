"""第二轮修复的专项回归测试。

覆盖 repair-progress.md 列出的待补齐证据：
- scheduler health 状态
- CookieCloud 请求体上限
- safe_request 响应体上限
- 媒体签名 URL 鉴权
- RSS 坏 enclosure.length 容错
- 站点重名 409
- 并发任务唯一索引与启动收敛
- RuntimeSettings 部分更新语义
"""

import asyncio
import gzip
import os
import socket
import tempfile
import time
import unittest
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.compat import main  # noqa: E402 (审计 2-12：测试兼容层) # type: ignore[import-not-found]
from app import main as app_main
from app.config import settings
from app.database import connect, initialize
from app.security import (
    MEDIA_SIGNATURE_TTL_SECONDS,
    media_signature_matches,
    signed_media_url,
)
from app.state import (
    _scheduler_last_heartbeat_monotonic,
    mark_scheduler_heartbeat,
    mark_scheduler_started,
    mark_scheduler_success,
    scheduler_health,
    scheduler_last_error,
    scheduler_last_heartbeat_at,
    scheduler_last_success_at,
    scheduler_running,
    scheduler_started_at,
)
from app.util import read_request_body_limited, safe_request, to_int

STRONG_TOKEN = "A9b8C7d6" * 4  # 32 字符，满足强度要求


from tests.support import IsolatedAppTestCase as _IsolatedTestCase  # noqa: N813 (审计 3-17：共享隔离样板)


class SchedulerHealthTests(_IsolatedTestCase):
    def _restore_scheduler_state(self) -> None:
        # 测试会修改进程级 scheduler 状态，通过模块对象恢复默认值。
        import app.state as state

        state.scheduler_running = False
        state.scheduler_started_at = None
        state.scheduler_last_heartbeat_at = None
        state.scheduler_last_success_at = None
        state.scheduler_last_error = None
        state._scheduler_last_heartbeat_monotonic = None

    async def test_health_endpoint_exposes_scheduler_snapshot(self) -> None:
        with TestClient(main.app) as client:
            health = client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        scheduler = health.json()["scheduler"]
        self.assertIn("running", scheduler)
        self.assertIn("status", scheduler)
        self.assertIn("last_heartbeat_at", scheduler)
        self.assertIn("last_success_at", scheduler)
        self.assertIn("last_error", scheduler)
        self.assertEqual(health.json()["scheduler_ok"], scheduler["ok"])

    async def test_scheduler_health_reports_stopped_when_never_started(self) -> None:
        self._restore_scheduler_state()
        snapshot = scheduler_health()
        self.assertFalse(snapshot["ok"])
        self.assertEqual(snapshot["status"], "stopped")
        self.assertFalse(snapshot["running"])
        self.assertIsNone(snapshot["last_heartbeat_at"])
        self.assertIsNone(snapshot["last_error"])

    async def test_scheduler_health_tracks_heartbeat_success_and_staleness(self) -> None:
        self._restore_scheduler_state()
        mark_scheduler_started("2026-08-10T00:00:00Z")
        fresh = scheduler_health()
        self.assertTrue(fresh["ok"])
        self.assertEqual(fresh["status"], "starting")
        mark_scheduler_heartbeat("2026-08-10T00:01:00Z", monotonic_now=100.0)
        beating = scheduler_health(monotonic_now=110.0)
        self.assertEqual(beating["status"], "ok")
        self.assertTrue(beating["ok"])
        self.assertEqual(beating["last_heartbeat_at"], "2026-08-10T00:01:00Z")
        mark_scheduler_success("2026-08-10T00:02:00Z")
        successful = scheduler_health(monotonic_now=110.0)
        self.assertEqual(successful["last_success_at"], "2026-08-10T00:02:00Z")
        # 超过 180 秒未心跳 → stale
        stale = scheduler_health(monotonic_now=300.0)
        self.assertEqual(stale["status"], "stale")
        self.assertFalse(stale["ok"])


class _FakeRequest:
    """最小 ASGI 风格请求：支持 headers 与异步 stream()。"""

    def __init__(self, chunks: list[bytes], content_length: str | None = None) -> None:
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = content_length
        self._chunks = chunks

    async def stream(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class CookieCloudBodyLimitTests(_IsolatedTestCase):
    async def test_read_request_body_limited_rejects_declared_oversize(self) -> None:
        request = _FakeRequest([b"x" * 2048], content_length="4096")
        with self.assertRaises(HTTPException) as raised:
            await read_request_body_limited(request, limit=2048)
        self.assertEqual(raised.exception.status_code, 413)

    async def test_read_request_body_limited_rejects_chunked_oversize_without_header(self) -> None:
        request = _FakeRequest([b"a" * 1500, b"b" * 1500])
        with self.assertRaises(HTTPException) as raised:
            await read_request_body_limited(request, limit=2048)
        self.assertEqual(raised.exception.status_code, 413)

    async def test_read_request_body_limited_accepts_exact_limit(self) -> None:
        payload = b"x" * 2048
        request = _FakeRequest([payload])
        self.assertEqual(await read_request_body_limited(request, limit=2048), payload)

    async def test_read_request_body_limited_rejects_invalid_content_length(self) -> None:
        request = _FakeRequest([b"x"], content_length="not-a-number")
        with self.assertRaises(HTTPException) as raised:
            await read_request_body_limited(request)
        self.assertEqual(raised.exception.status_code, 400)


class RequestBodyLimitTests(_IsolatedTestCase):
    async def test_import_routes_use_schema_sized_http_limit(self) -> None:
        with TestClient(main.app) as client:
            ordinary = client.post(
                "/api/config/score-preview", content=b"{}",
                headers={"content-length": str(app_main.MAX_REQUEST_BODY_BYTES + 1)},
            )
            import_allowed = client.post(
                "/api/playlists/import", content=b"{}",
                headers={"content-length": str(app_main.MAX_REQUEST_BODY_BYTES + 1)},
            )
            import_rejected = client.post(
                "/api/playlists/import/preview", content=b"{}",
                headers={"content-length": str(app_main.MAX_IMPORT_HTTP_BODY_BYTES + 1)},
            )
        self.assertEqual(ordinary.status_code, 413)
        self.assertNotEqual(import_allowed.status_code, 413)
        self.assertEqual(import_rejected.status_code, 413)

    async def test_non_get_without_content_length_is_rejected(self) -> None:
        async def body():
            yield b"{}"

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/playlists/import", content=body())
        self.assertEqual(response.status_code, 411)


class SafeRequestResponseLimitTests(_IsolatedTestCase):
    async def test_safe_request_rejects_declared_oversize_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 1024, headers={"content-length": "1024"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(RuntimeError, "响应过大"):
                await safe_request(client, "GET", "https://feed.example/rss", max_response_bytes=512)

    async def test_safe_request_rejects_actual_oversize_body_without_length_header(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 1024)  # 无 content-length

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(RuntimeError, "响应过大"):
                await safe_request(client, "GET", "https://feed.example/rss", max_response_bytes=512)

    async def test_safe_request_tolerates_invalid_content_length_when_body_is_small(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"small", headers={"content-length": "garbage"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await safe_request(client, "GET", "https://feed.example/rss", max_response_bytes=1024)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"small")

    async def test_safe_request_rejects_invalid_size_cap(self) -> None:
        async with httpx.AsyncClient() as client:
            with self.assertRaises(ValueError):
                await safe_request(client, "GET", "https://feed.example/rss", max_response_bytes=0)

    async def test_safe_request_streams_asyncmock_response_incrementally(self) -> None:
        class StreamResponse:
            status_code = 200
            headers: dict[str, str] = {}

            async def aiter_bytes(self):
                yield b"first-"
                yield b"second"

        client = Mock()
        client.get = AsyncMock(return_value=StreamResponse())
        response = await safe_request(
            client, "GET", "https://feed.example/rss", allow_private=True, max_response_bytes=32,
        )
        client.get.assert_awaited_once()
        self.assertEqual(response._content, b"first-second")

    async def test_safe_request_keeps_buffered_asyncmock_response_compatible(self) -> None:
        response = AsyncMock()
        response.status_code = 200
        response.headers = {}
        response.content = b"buffered"
        client = Mock()
        client.get = AsyncMock(return_value=response)
        result = await safe_request(
            client, "GET", "https://feed.example/rss", allow_private=True, max_response_bytes=32,
        )
        self.assertEqual(result.content, b"buffered")

    async def test_safe_request_accepts_asyncmock_stream_method(self) -> None:
        response = httpx.Response(200, content=b"stream-mock")
        client = httpx.AsyncClient()
        try:
            with patch.object(client, "stream", new=AsyncMock(return_value=response)) as stream:
                result = await safe_request(
                    client, "GET", "https://feed.example/rss", allow_private=True, max_response_bytes=32,
                )
            stream.assert_awaited_once()
            self.assertEqual(result.content, b"stream-mock")
        finally:
            await client.aclose()


class SafeRequestRedirectTests(_IsolatedTestCase):
    async def test_303_converts_post_to_get_and_clears_body(self) -> None:
        seen: list[tuple[str, bytes, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.content, request.headers.get("content-type")))
            if request.url.path == "/start":
                return httpx.Response(303, headers={"Location": "https://other.example/final"})
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            response = await safe_request(
                client, "POST", "https://source.example/start", json={"secret": "body"}, allow_private=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen[0][0], "POST")
        self.assertIn(b"secret", seen[0][1])
        self.assertEqual(seen[1][0], "GET")
        self.assertEqual(seen[1][1], b"")
        self.assertIsNone(seen[1][2])

    async def test_301_and_302_explicitly_convert_post_to_get(self) -> None:
        for redirect_status in (301, 302):
            seen: list[tuple[str, bytes]] = []

            def handler(request: httpx.Request, status: int = redirect_status) -> httpx.Response:
                seen.append((request.method, request.content))
                if request.url.path == "/start":
                    return httpx.Response(status, headers={"Location": "https://other.example/final"})
                return httpx.Response(200)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
                response = await safe_request(
                    client, "POST", "https://source.example/start", content=b"private-body", allow_private=True,
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(seen, [("POST", b"private-body"), ("GET", b"")])

    async def test_cross_origin_307_and_308_with_body_are_blocked(self) -> None:
        for redirect_status in (307, 308):
            seen: list[tuple[str, bytes]] = []

            def handler(request: httpx.Request, status: int = redirect_status) -> httpx.Response:
                seen.append((request.method, request.content))
                return httpx.Response(status, headers={"Location": "https://other.example/final"})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
                with self.assertRaisesRegex(RuntimeError, "跨域 307/308 重定向携带请求体"):
                    await safe_request(
                        client, "POST", "https://source.example/start", json={"secret": "body"}, allow_private=True,
                    )
            self.assertEqual(seen, [("POST", b'{"secret":"body"}')])

    async def test_same_origin_307_preserves_post_body(self) -> None:
        seen: list[tuple[str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.content))
            if request.url.path == "/start":
                return httpx.Response(307, headers={"Location": "/final"})
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            response = await safe_request(
                client, "POST", "https://source.example/start", content=b"private-body", allow_private=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [("POST", b"private-body"), ("POST", b"private-body")])


class MediaSignatureTests(_IsolatedTestCase):
    async def test_signed_media_url_passthrough_without_token(self) -> None:
        os.environ.pop("AUTOLIST_ACCESS_TOKEN", None)
        self.assertEqual(signed_media_url("/api/sites/7/icon"), "/api/sites/7/icon")

    async def test_signed_media_url_roundtrip_and_rejects_tampering(self) -> None:
        os.environ["AUTOLIST_ACCESS_TOKEN"] = STRONG_TOKEN
        path = "/api/sites/7/icon"
        url = signed_media_url(path)
        self.assertIn("expires=", url)
        self.assertIn("signature=", url)
        self.assertNotIn(STRONG_TOKEN, url)
        query = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))
        self.assertTrue(media_signature_matches(path, query["expires"], query["signature"]))
        # 篡改签名
        self.assertFalse(media_signature_matches(path, query["expires"], "0" * 64))
        # 错误路径
        self.assertFalse(media_signature_matches("/api/sites/8/icon", query["expires"], query["signature"]))
        # 过期
        past = str(to_int(time.time()) - MEDIA_SIGNATURE_TTL_SECONDS - 60)
        self.assertFalse(media_signature_matches(path, past, query["signature"]))
        # 缺失参数
        self.assertFalse(media_signature_matches(path, None, None))
        self.assertFalse(media_signature_matches(path, "not-a-number", query["signature"]))

    async def test_poster_signature_with_query_tag_validates_against_bare_path(self) -> None:
        # 签名只覆盖不含查询的 path，与中间件用 request.url.path 验证的契约一致；
        # 海报 URL 携带的 ?tag= 缓存参数不得破坏签名校验。
        os.environ["AUTOLIST_ACCESS_TOKEN"] = STRONG_TOKEN
        path = "/api/playlist-items/1/poster"
        signed = signed_media_url(f"{path}?tag=tag1")
        query = dict(part.split("=", 1) for part in signed.split("?", 1)[1].split("&"))
        self.assertTrue(media_signature_matches(path, query["expires"], query["signature"]))

    async def test_media_endpoint_requires_signature_when_token_enabled(self) -> None:
        os.environ["AUTOLIST_ACCESS_TOKEN"] = STRONG_TOKEN
        path = "/api/sites/7/icon"
        # 填充图标缓存，避免测试触发真实网络请求；签名校验发生在中间件。
        from app.state import remember_site_icon

        remember_site_icon(7, (b"icon", "image/x-icon"))
        with TestClient(main.app) as client:
            unsigned = client.get(path)
            signed_url = signed_media_url(path)
            signed = client.get(signed_url)
        self.assertEqual(unsigned.status_code, 401)
        self.assertEqual(signed.status_code, 200)
        self.assertEqual(signed.content, b"icon")

    async def test_signed_media_cannot_bypass_strict_weak_token_mode(self) -> None:
        os.environ["AUTOLIST_ACCESS_TOKEN"] = "weak-token"
        os.environ["AUTOLIST_REQUIRE_STRONG_TOKEN"] = "true"
        from app.state import remember_site_icon

        remember_site_icon(7, (b"icon", "image/x-icon"))
        signed_url = signed_media_url("/api/sites/7/icon")
        with TestClient(main.app) as client:
            blocked = client.get(signed_url)
        self.assertEqual(blocked.status_code, 503)

    async def test_media_endpoint_stays_open_without_token(self) -> None:
        os.environ.pop("AUTOLIST_ACCESS_TOKEN", None)
        from app.state import remember_site_icon

        remember_site_icon(7, (b"icon", "image/x-icon"))
        with TestClient(main.app) as client:
            plain = client.get("/api/sites/7/icon")
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(plain.content, b"icon")

    async def test_poster_endpoint_accepts_signed_url_with_tag_query(self) -> None:
        # token 部署下海报 URL 带 ?tag= 缓存参数，签名覆盖的路径必须与中间件
        # 验证的 request.url.path 一致，否则 dashboard 海报恒 401。
        os.environ["AUTOLIST_ACCESS_TOKEN"] = STRONG_TOKEN
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("签名海报片单", 1, main.utc_now()),
            ).lastrowid)
            conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,original_title,year,emby_item_id,emby_image_tag)
                   VALUES(?,?,?,?,?,?)""",
                (playlist_id, 1, "Poster Movie", 2020, "emby-item-42", "tag1"),
            )
        from app.state import remember_poster

        remember_poster("emby-item-42:tag1", (b"\x89PNG\r\n\x1a\nposter", "image/png"))
        signed = signed_media_url("/api/playlist-items/1/poster?tag=tag1")
        with TestClient(main.app) as client:
            unsigned = client.get("/api/playlist-items/1/poster?tag=tag1")
            authorized = client.get(signed)
        self.assertEqual(unsigned.status_code, 401)
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.content, b"\x89PNG\r\n\x1a\nposter")


class RSSBadEnclosureTests(_IsolatedTestCase):
    async def test_rss_client_tolerates_non_numeric_enclosure_length(self) -> None:
        from app.clients import RSSClient

        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><item>
          <title>Movie 2020 1080p WEB-DL</title>
          <enclosure url="https://tracker.example/detail/torrent.torrent" length="not-a-number"/>
        </item><item>
          <title>Movie 2020 2160p REMUX</title>
          <enclosure url="https://tracker.example/detail/torrent2.torrent" length="1.5GB"/>
        </item><item>
          <title>Movie 2020 720p</title>
          <enclosure url="https://tracker.example/detail/torrent3.torrent"/>
        </item></channel></rss>"""
        response = Mock()
        response.status_code = 200
        response.content = xml
        response.headers = {}
        response.raise_for_status = Mock()
        site = {
            "name": "RSS 测试站", "rss_url": "https://feed.example/rss.xml", "user_agent": "AutoList",
            "cookie": "", "timeout_seconds": 30,
        }
        with patch("app.clients.safe_request", new=AsyncMock(return_value=response)) as request:
            results = await RSSClient().search(site, "Movie 2020")
        request.assert_awaited_once()
        self.assertEqual(len(results), 3)
        self.assertEqual([item["size"] for item in results], [0, 0, 0])
        self.assertEqual(results[0]["site_name"], "RSS 测试站")


class DuplicateSiteNameTests(_IsolatedTestCase):
    async def test_duplicate_site_name_returns_409_not_500(self) -> None:
        payload = {"name": "重复站点", "base_url": "https://tracker.example"}
        with TestClient(main.app) as client:
            first = client.post("/api/sites", json=payload)
            second = client.post("/api/sites", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"], "站点名称已存在")

    async def test_rename_to_existing_name_returns_409(self) -> None:
        with TestClient(main.app) as client:
            first = client.post("/api/sites", json={"name": "站点甲", "base_url": "https://tracker-a.example"})
            second = client.post("/api/sites", json={"name": "站点乙", "base_url": "https://tracker-b.example"})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            site_id = second.json()["id"]
            renamed = client.put(
                f"/api/sites/{site_id}",
                json={"name": "站点甲", "base_url": "https://tracker-b.example", "clear_api_key": False, "clear_cookie": False, "clear_rss_url": False},
            )
        self.assertEqual(renamed.status_code, 409)


class ActiveTaskUniquenessTests(_IsolatedTestCase):
    async def test_unique_partial_indexes_exist_after_initialize(self) -> None:
        with connect() as conn:
            indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for expected in (
            "idx_automation_active_playlist",
            "idx_recognition_active_playlist",
            "idx_library_scan_active_playlist",
        ):
            self.assertIn(expected, indexes)

    async def test_duplicate_active_automation_task_is_rejected_by_index(self) -> None:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("并发片单", 1, main.utc_now()),
            ).lastrowid)
            now = main.utc_now()
            conn.execute(
                """INSERT INTO automation_runs(playlist_id,status,trigger,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "queued", "manual", now, now),
            )
            with self.assertRaises(Exception) as raised:
                conn.execute(
                    """INSERT INTO automation_runs(playlist_id,status,trigger,created_at,updated_at)
                       VALUES(?,?,?,?,?)""",
                    (playlist_id, "running", "manual", now, now),
                )
        self.assertIn("UNIQUE", str(raised.exception).upper())

    async def test_initialize_retires_legacy_duplicate_active_tasks(self) -> None:
        # 模拟旧版本留下的重复 active 任务：先删掉唯一索引再插入两条。
        with connect() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_automation_active_playlist")
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("遗留片单", 1, main.utc_now()),
            ).lastrowid)
            now = main.utc_now()
            conn.execute(
                """INSERT INTO automation_runs(playlist_id,status,trigger,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "queued", "manual", now, now),
            )
            conn.execute(
                """INSERT INTO automation_runs(playlist_id,status,trigger,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "running", "scheduler", now, now),
            )
        initialize()
        with connect() as conn:
            rows = conn.execute(
                "SELECT id,status,message FROM automation_runs WHERE playlist_id=? ORDER BY id",
                (playlist_id,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "interrupted")
        self.assertIn("重复", rows[0]["message"] or "")
        self.assertEqual(rows[1]["status"], "running")


class RuntimeSettingsPartialUpdateTests(_IsolatedTestCase):
    async def test_partial_update_keeps_omitted_values_and_nulls_preserve_existing(self) -> None:
        with TestClient(main.app) as client:
            baseline = client.get("/api/settings").json()
            self.assertEqual(baseline["mp_base_url"], "")
            self.assertFalse(baseline["mp_api_key_configured"])
            updated = client.put("/api/settings", json={"mp_base_url": "https://mp.example", "mp_api_key": "secret-key"})
            self.assertEqual(updated.status_code, 200)
            after = client.get("/api/settings").json()
            self.assertEqual(after["mp_base_url"], "https://mp.example")
            self.assertTrue(after["mp_api_key_configured"])
            # 显式 null 保留原值（设置页留空字段不清除已保存密钥）
            kept = client.put("/api/settings", json={"mp_api_key": None})
            self.assertEqual(kept.status_code, 200)
            kept_after = client.get("/api/settings").json()
            self.assertTrue(kept_after["mp_api_key_configured"])
            # 显式空字符串清除密钥
            cleared = client.put("/api/settings", json={"mp_api_key": "", "emby_base_url": ""})
            self.assertEqual(cleared.status_code, 200)
            cleared_after = client.get("/api/settings").json()
            self.assertFalse(cleared_after["mp_api_key_configured"])
            self.assertEqual(cleared_after["emby_base_url"], "")

    async def test_partial_update_validates_provided_urls_only(self) -> None:
        with TestClient(main.app) as client:
            baseline = client.get("/api/settings").json()
            bad = client.put("/api/settings", json={"tr_base_url": "not a url"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(baseline["tr_base_url"], "")


class DNSRebindingTests(_IsolatedTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import app.util as util_module
        util_module._dns_address_cache.clear()

    async def asyncTearDown(self) -> None:
        import app.util as util_module
        util_module._dns_address_cache.clear()
        await super().asyncTearDown()

    async def test_bind_outbound_host_pins_hostname_and_preserves_ip_literals(self) -> None:
        from app.util import _bind_outbound_host

        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch("app.util.socket.getaddrinfo", return_value=info):
            bound, host = await _bind_outbound_host(
                "https://api.example.org/v1/data", allow_private=False,
            )
        self.assertEqual(bound, "https://8.8.8.8/v1/data")
        self.assertEqual(host, "api.example.org")
        # IP 字面量不再解析，原样返回
        literal, literal_host = await _bind_outbound_host("https://8.8.8.8/v1", allow_private=False)
        self.assertEqual(literal, "https://8.8.8.8/v1")
        self.assertIsNone(literal_host)
        # 私有信任模式不绑定
        trusted, trusted_host = await _bind_outbound_host("https://intra.example/v1", allow_private=True)
        self.assertEqual(trusted, "https://intra.example/v1")
        self.assertIsNone(trusted_host)

    async def test_bind_outbound_host_rejects_private_resolution(self) -> None:
        from app.util import _bind_outbound_host

        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443))]
        with patch("app.util.socket.getaddrinfo", return_value=info):
            with self.assertRaisesRegex(ValueError, "内网"):
                await _bind_outbound_host("https://api.example.org/v1", allow_private=False)

    async def test_safe_request_pins_connection_target_and_preserves_host_and_sni(self) -> None:
        seen: list[tuple[str, str | None, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), request.headers.get("host"), request.extensions.get("sni_hostname")))
            return httpx.Response(200, json={"ok": True})

        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 8443))]
        with patch("app.util.socket.getaddrinfo", return_value=info):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                response = await safe_request(
                    client, "GET", "https://api.example.org:8443/v1/data", allow_private=False, max_redirects=0,
                )
        self.assertEqual(response.status_code, 200)
        url, host, sni = seen[0]
        # 连接目标必须是刚校验过的 IP，而不是域名（httpx 不会再自行解析）。
        self.assertTrue(url.startswith("https://8.8.8.8:8443/"), url)
        self.assertEqual(host, "api.example.org:8443")
        self.assertEqual(sni, "api.example.org")

    async def test_safe_request_keeps_host_pinning_across_relative_redirect(self) -> None:
        seen: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), request.headers.get("host")))
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "/next"})
            return httpx.Response(200, json={"ok": True})

        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch("app.util.socket.getaddrinfo", return_value=info):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                response = await safe_request(client, "GET", "https://api.example.org/start", allow_private=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[1][0].startswith("https://8.8.8.8/next"), seen[1][0])
        self.assertEqual(seen[1][1], "api.example.org")

    async def test_safe_request_rejects_domain_resolving_to_private(self) -> None:
        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("app.util.socket.getaddrinfo", return_value=info):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
                with self.assertRaisesRegex(Exception, "内网"):
                    await safe_request(client, "GET", "https://api.example.org/v1", allow_private=False)

    async def test_safe_request_does_not_pin_connection_target_through_proxy(self) -> None:
        # 代理场景 DNS 由可信代理解析：不得把目标改写成 IP 字面量，否则
        # CONNECT 目标、虚拟主机与 TLS SNI 全部失效。
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            with patch("app.util._bind_outbound_host", new=AsyncMock(return_value=("https://8.8.8.8/v1", "api.example.org"))) as bind, \
                 patch("app.util.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]):
                response = await safe_request(
                    client, "GET", "https://api.example.org/v1", allow_private=False,
                    proxy_mode=True, max_redirects=0,
                )
            self.assertEqual(response.status_code, 200)
            bind.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class RouteCoverageTests(_IsolatedTestCase):
    """2-18/2-19：取消任务、通知与设置测试路由的最小行为测试。"""

    async def _seed_search_task(self, status: str = "queued") -> int:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("路由覆盖片单", 1, main.utc_now()),
            ).lastrowid)
            return to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,trigger,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, status, 1, "manual", main.utc_now(), main.utc_now()),
            ).lastrowid)

    async def test_cancel_task_rejects_unknown_and_finished(self) -> None:
        with TestClient(main.app) as client:
            missing = client.post("/api/search-tasks/999999/cancel")
            self.assertEqual(missing.status_code, 404)
            finished_id = await self._seed_search_task("completed")
            finished = client.post(f"/api/search-tasks/{finished_id}/cancel")
            self.assertEqual(finished.status_code, 409)

    async def test_cancel_task_cancels_running_db_task(self) -> None:
        # 任务须在 TestClient（lifespan）启动之后再插入：启动时会把存量
        # queued/running 任务置为 interrupted。
        with TestClient(main.app) as client:
            task_id = await self._seed_search_task("running")
            response = client.post(f"/api/search-tasks/{task_id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        with connect() as conn:
            row = conn.execute("SELECT status FROM search_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(row["status"], "cancelled")

    async def test_notifications_route_and_read_all(self) -> None:
        from app.services.automation import add_notification
        add_notification("测试通知", "内容")
        with TestClient(main.app) as client:
            listing = client.get("/api/notifications")
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(listing.json()[0]["title"], "测试通知")
            unread = client.get("/api/notifications?unread_only=true")
            self.assertEqual(unread.json()[0]["read"], 0)
            marked = client.post("/api/notifications/read-all")
            self.assertEqual(marked.status_code, 200)
            self.assertEqual(marked.json()["updated"], 1)

    async def test_score_preview_route_returns_analysis(self) -> None:
        from app.candidate_policy import DEFAULT_POLICY
        with TestClient(main.app) as client:
            response = client.post("/api/config/score-preview", json={
                "title": "Movie.2024.1080p.BluRay.x265-FRDS",
                "candidate_policy": DEFAULT_POLICY,
            })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("recommendation", body)
        self.assertIn("ranking", body)
        self.assertEqual(body["group"], "FRDS")


class RouteCoverageExpansionTests(_IsolatedTestCase):
    """2-19：补齐剩余零覆盖路由的最小行为断言。"""

    async def _playlist(self) -> int:
        with connect() as conn:
            return to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("路由扩展片单", 1, main.utc_now()),
            ).lastrowid)

    async def test_connection_and_downloads_endpoints(self) -> None:
        with TestClient(main.app) as client:
            connection = client.get("/api/connection")
            self.assertEqual(connection.status_code, 200)
            self.assertIn("providers", connection.json())
            downloads = client.get("/api/downloads")
            self.assertEqual(downloads.status_code, 200)
            self.assertIsInstance(downloads.json(), list)
