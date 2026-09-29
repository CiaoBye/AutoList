"""安全：凭据脱敏、访问令牌、请求体与响应体上限、出站地址校验与 DNS 固定、媒体签名。"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from defusedxml import ElementTree
from defusedxml.common import EntitiesForbidden
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main as app_main
from app.api import system as system_routes
from app.config import (
    ACCESS_TOKEN_MIN_LENGTH,
    access_token_is_strong,
    access_token_strength,
    access_token_strength_enforced,
    access_token_validation_error,
    settings,
)
from app.database import connect
from app.main import app
from app.outbound import safe_detail_url, safe_request, validate_outbound_url
from app.schemas import ImportPayload, TaskPayload
from app.security import (
    MEDIA_SIGNATURE_TTL_SECONDS,
    media_signature_matches,
    safe_error,
    sanitize_sensitive_text,
    signed_media_url,
)
from app.util import secret_free, to_int, utc_now
from tests.support import IsolatedAppTestCase, SeededPlaylistTestCase

STRONG_TOKEN = "A9b8C7d6" * 4  # 32 字符，满足强度要求

class RedactionAndAccessTokenTests(unittest.TestCase):
    def test_sensitive_values_are_redacted_in_urls_headers_and_json(self) -> None:
        message = (
            "GET https://user:password@tracker.test/download?passkey=secret&api_key=key "
            "headers={'Authorization': 'Bearer abc.def', 'Cookie': 'uid=1; token=two'}"
        )
        sanitized = sanitize_sensitive_text(message)
        for secret in ("password@", "passkey=secret", "api_key=key", "abc.def", "uid=1", "token=two"):
            self.assertNotIn(secret, sanitized)
        self.assertIn("passkey=***", sanitized)
        self.assertIn("'Cookie': '***'", sanitized)

    def test_untrusted_xml_entities_are_rejected(self) -> None:
        malicious = '<!DOCTYPE data [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><data>&xxe;</data>'
        with self.assertRaises(EntitiesForbidden):
            ElementTree.fromstring(malicious)

    def test_secret_free_redacts_download_urls_and_magnets(self) -> None:
        safe = secret_free({
            "enclosure": "https://tracker.example/download?passkey=secret",
            "magnet": "magnet:?xt=urn:btih:privatehash",
            "description": "详情见 https://tracker.example/details?id=1",
            "labels": ["FREE"],
        })
        self.assertNotIn("secret", json.dumps(safe, ensure_ascii=False))
        self.assertNotIn("privatehash", json.dumps(safe, ensure_ascii=False))
        self.assertNotIn("https://tracker.example/details", json.dumps(safe, ensure_ascii=False))
        self.assertEqual(safe["labels"], ["FREE"])

    def test_access_token_helpers_use_constant_time_compare(self) -> None:
        from app.security import extract_access_token, token_matches

        self.assertTrue(token_matches("secret-token", "secret-token"))
        self.assertFalse(token_matches("secret-token", "other-token"))
        self.assertFalse(token_matches("", "secret-token"))
        self.assertEqual(extract_access_token("Bearer abc123", None), "abc123")
        self.assertEqual(extract_access_token(None, " header-token "), "header-token")

    def test_access_token_middleware_protects_api_but_keeps_health_open(self) -> None:
        previous_token = os.environ.get("AUTOLIST_ACCESS_TOKEN")
        previous_strict = os.environ.get("AUTOLIST_REQUIRE_STRONG_TOKEN")
        previous_data_dir = settings.data_dir
        temp = tempfile.TemporaryDirectory()
        os.environ["AUTOLIST_ACCESS_TOKEN"] = "unit-test-token"
        os.environ["AUTOLIST_REQUIRE_STRONG_TOKEN"] = "false"
        settings.data_dir = temp.name
        try:
            with TestClient(app) as client:
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertTrue(health.json().get("access_token_required"))
                self.assertEqual(client.get("/favicon.ico").status_code, 200)
                denied = client.get("/api/settings")
                self.assertEqual(denied.status_code, 401)
                allowed = client.get("/api/settings", headers={"X-AutoList-Token": "unit-test-token"})
                self.assertEqual(allowed.status_code, 200)
                bearer = client.get("/api/settings", headers={"Authorization": "Bearer unit-test-token"})
                self.assertEqual(bearer.status_code, 200)
        finally:
            settings.data_dir = previous_data_dir
            if previous_token is None:
                os.environ.pop("AUTOLIST_ACCESS_TOKEN", None)
            else:
                os.environ["AUTOLIST_ACCESS_TOKEN"] = previous_token
            if previous_strict is None:
                os.environ.pop("AUTOLIST_REQUIRE_STRONG_TOKEN", None)
            else:
                os.environ["AUTOLIST_REQUIRE_STRONG_TOKEN"] = previous_strict
            temp.cleanup()


class BaseUrlValidationTests(SeededPlaylistTestCase):
    async def test_validated_base_url_rejects_private_only_domain_with_token(self) -> None:
        """启用访问令牌时，出站地址域名必须全部解析到公网；白名单与 IP 字面量规则生效。"""
        previous_token = os.environ.get("AUTOLIST_ACCESS_TOKEN")
        os.environ["AUTOLIST_ACCESS_TOKEN"] = "test-token"
        previous_hosts = os.environ.get("AUTOLIST_ALLOW_PRIVATE_HOSTS")
        try:
            with patch("app.outbound.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.5", 0))]):
                with self.assertRaises(HTTPException) as raised:
                    system_routes.validated_base_url("https://private.example", "站点地址", True)
                self.assertEqual(raised.exception.status_code, 422)
            with patch("app.outbound.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
                result = system_routes.validated_base_url("https://public.example", "站点地址", True)
            self.assertEqual(result, "https://public.example")
            with patch("app.outbound.socket.getaddrinfo", side_effect=socket.gaierror("nxdomain")):
                with self.assertRaises(HTTPException):
                    system_routes.validated_base_url("https://nx.example", "站点地址", True)
            os.environ["AUTOLIST_ALLOW_PRIVATE_HOSTS"] = "prowlarr.lan"
            with patch("app.outbound.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.9", 0))]):
                result = system_routes.validated_base_url("https://prowlarr.lan", "站点地址", True)
            self.assertEqual(result, "https://prowlarr.lan")
        finally:
            if previous_hosts is None:
                os.environ.pop("AUTOLIST_ALLOW_PRIVATE_HOSTS", None)
            else:
                os.environ["AUTOLIST_ALLOW_PRIVATE_HOSTS"] = previous_hosts
            if previous_token is None:
                os.environ.pop("AUTOLIST_ACCESS_TOKEN", None)
            else:
                os.environ["AUTOLIST_ACCESS_TOKEN"] = previous_token


class AccessTokenAndOutboundTests(IsolatedAppTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        settings.tr_base_url = ""
        settings.emby_base_url = ""
        settings.emby_api_key = ""

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
        with TestClient(app) as client:
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
        with TestClient(app) as client:
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

    def test_search_and_import_payloads_have_server_limits(self) -> None:
        with self.assertRaises(ValidationError):
            TaskPayload(playlist_id=1, scope="range", range_start=1, range_end=2001)
        with self.assertRaises(ValidationError):
            ImportPayload(json_data={"films": [{"title": "x", "description": "x" * (10 * 1024 * 1024)}]})


class RequestBodyLimitTests(IsolatedAppTestCase):
    async def test_import_routes_use_schema_sized_http_limit(self) -> None:
        with TestClient(app) as client:
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

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/playlists/import", content=body())
        self.assertEqual(response.status_code, 411)


class SafeRequestResponseLimitTests(IsolatedAppTestCase):
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


class SafeRequestRedirectTests(IsolatedAppTestCase):
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


class MediaSignatureTests(IsolatedAppTestCase):
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
        with TestClient(app) as client:
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
        with TestClient(app) as client:
            blocked = client.get(signed_url)
        self.assertEqual(blocked.status_code, 503)

    async def test_media_endpoint_stays_open_without_token(self) -> None:
        os.environ.pop("AUTOLIST_ACCESS_TOKEN", None)
        from app.state import remember_site_icon

        remember_site_icon(7, (b"icon", "image/x-icon"))
        with TestClient(app) as client:
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
                ("签名海报片单", 1, utc_now()),
            ).lastrowid)
            conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,original_title,year,emby_item_id,emby_image_tag)
                   VALUES(?,?,?,?,?,?)""",
                (playlist_id, 1, "Poster Movie", 2020, "emby-item-42", "tag1"),
            )
        from app.state import remember_poster

        remember_poster("emby-item-42:tag1", (b"\x89PNG\r\n\x1a\nposter", "image/png"))
        signed = signed_media_url("/api/playlist-items/1/poster?tag=tag1")
        with TestClient(app) as client:
            unsigned = client.get("/api/playlist-items/1/poster?tag=tag1")
            authorized = client.get(signed)
        self.assertEqual(unsigned.status_code, 401)
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.content, b"\x89PNG\r\n\x1a\nposter")


class DNSRebindingTests(IsolatedAppTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import app.outbound as outbound_module
        outbound_module._dns_address_cache.clear()

    async def asyncTearDown(self) -> None:
        import app.outbound as outbound_module
        outbound_module._dns_address_cache.clear()
        await super().asyncTearDown()

    async def test_bind_outbound_host_pins_hostname_and_preserves_ip_literals(self) -> None:
        from app.outbound import _bind_outbound_host

        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch("app.outbound.socket.getaddrinfo", return_value=info):
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
        from app.outbound import _bind_outbound_host

        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443))]
        with patch("app.outbound.socket.getaddrinfo", return_value=info):
            with self.assertRaisesRegex(ValueError, "内网"):
                await _bind_outbound_host("https://api.example.org/v1", allow_private=False)

    async def test_safe_request_pins_connection_target_and_preserves_host_and_sni(self) -> None:
        seen: list[tuple[str, str | None, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), request.headers.get("host"), request.extensions.get("sni_hostname")))
            return httpx.Response(200, json={"ok": True})

        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 8443))]
        with patch("app.outbound.socket.getaddrinfo", return_value=info):
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
        with patch("app.outbound.socket.getaddrinfo", return_value=info):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                response = await safe_request(client, "GET", "https://api.example.org/start", allow_private=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[1][0].startswith("https://8.8.8.8/next"), seen[1][0])
        self.assertEqual(seen[1][1], "api.example.org")

    async def test_safe_request_rejects_domain_resolving_to_private(self) -> None:
        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("app.outbound.socket.getaddrinfo", return_value=info):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
                with self.assertRaisesRegex(Exception, "内网"):
                    await safe_request(client, "GET", "https://api.example.org/v1", allow_private=False)

    async def test_safe_request_does_not_pin_connection_target_through_proxy(self) -> None:
        # 代理场景 DNS 由可信代理解析：不得把目标改写成 IP 字面量，否则
        # CONNECT 目标、虚拟主机与 TLS SNI 全部失效。
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            with patch("app.outbound._bind_outbound_host", new=AsyncMock(return_value=("https://8.8.8.8/v1", "api.example.org"))) as bind, \
                 patch("app.outbound.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]):
                response = await safe_request(
                    client, "GET", "https://api.example.org/v1", allow_private=False,
                    proxy_mode=True, max_redirects=0,
                )
            self.assertEqual(response.status_code, 200)
            bind.assert_not_awaited()


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


class TokenFailureRateLimitTests(IsolatedAppTestCase):
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
