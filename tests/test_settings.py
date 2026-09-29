"""设置：运行设置的保存与清除、脱敏展示、服务连接检测。"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import system as system_routes
from app.api.system import RUNTIME_SETTING_CLEAR_FIELDS, validated_base_url
from app.clients import TransmissionClient
from app.config import load_runtime_settings, save_runtime_settings, settings
from app.main import app
from tests.support import IsolatedAppTestCase, SeededPlaylistTestCase

ROOT = Path(__file__).resolve().parent.parent

def run_payload_module(expression: str) -> object:
    """用 Node 直接执行 frontend/src/settingsPayload.ts，返回表达式的 JSON 结果。"""
    script = f"import * as m from './frontend/src/settingsPayload.ts'; console.log(JSON.stringify({expression}));"
    output = subprocess.check_output(
        ["node", "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", script],
        cwd=ROOT, text=True,
    )
    return json.loads(output)


class ServiceUrlSettingsTests(unittest.TestCase):
    def test_service_urls_reject_secret_query_parameters(self) -> None:
        for query in ("token=secret", "passkey=secret", "api_key=secret", "key=secret"):
            with self.subTest(query=query):
                with self.assertRaises(HTTPException):
                    validated_base_url(f"https://service.example/api?{query}", "服务地址", False)

    def test_public_settings_show_proxy_address_without_proxy_credentials(self) -> None:
        previous = settings.outbound_proxy_url
        settings.outbound_proxy_url = "http://192.168.31.99:1080"
        try:
            public = settings.public_values()
            self.assertEqual(public["outbound_proxy_url"], "http://192.168.31.99:1080")
            self.assertTrue(public["outbound_proxy_url_configured"])

            settings.outbound_proxy_url = "http://proxy-user:proxy-pass@example.test:8080"
            protected = settings.public_values()
        finally:
            settings.outbound_proxy_url = previous
        self.assertEqual(protected["outbound_proxy_url"], "")
        self.assertTrue(protected["outbound_proxy_url_configured"])


class RuntimeSettingsApiTests(SeededPlaylistTestCase):
    async def test_runtime_settings_clear_protocol_invalidates_connection_cache(self) -> None:
        previous_values = {
            "tmdb_api_key": settings.tmdb_api_key,
            "outbound_proxy_url": settings.outbound_proxy_url,
        }
        settings.tmdb_api_key = "tmdb-secret"
        settings.outbound_proxy_url = "http://192.168.31.99:1080"
        system_routes._connection_cache = (0.0, {"providers": {"stale": True}})
        try:
            with TestClient(app) as client:
                response = client.put(
                    "/api/settings",
                    json={"clear_tmdb_api_key": True, "clear_outbound_proxy_url": True},
                )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(settings.tmdb_api_key)
            self.assertFalse(settings.outbound_proxy_url)
            self.assertIsNone(system_routes._connection_cache)
        finally:
            settings.tmdb_api_key = previous_values["tmdb_api_key"]
            settings.outbound_proxy_url = previous_values["outbound_proxy_url"]

    async def test_settings_test_endpoint_forces_connection_refresh(self) -> None:
        with patch.object(
            system_routes, "connection", new=AsyncMock(return_value={"providers": {"fresh": True}})
        ) as connection_mock:
            result = await system_routes.test_runtime_settings()
        # “检测全部”在核心服务之外附带 Fanart（未配置时如实返回未配置）。
        self.assertEqual(result["fresh"], True)
        self.assertEqual(result["fanart"]["configured"], bool(system_routes.settings.fanart_api_key))
        connection_mock.assert_awaited_once_with(force_refresh=True)

    async def test_runtime_settings_concurrent_partial_saves_are_atomic(self) -> None:
        previous_language, previous_model = settings.tmdb_language, settings.ai_model
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(save_runtime_settings, {"tmdb_language": "en-US"}),
                    executor.submit(save_runtime_settings, {"ai_model": "audit-model"}),
                ]
                for future in futures:
                    future.result(timeout=3)
            payload = json.loads((Path(self.temp.name) / "runtime-settings.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["tmdb_language"], "en-US")
            self.assertEqual(payload["ai_model"], "audit-model")
            self.assertEqual(list(Path(self.temp.name).glob("runtime-settings.json.*.tmp")), [])
        finally:
            settings.tmdb_language, settings.ai_model = previous_language, previous_model

    async def test_transmission_allows_an_unauthenticated_rpc(self) -> None:
        previous = (settings.tr_base_url, settings.tr_username, settings.tr_password)
        try:
            settings.tr_base_url = "http://127.0.0.1:9091"
            settings.tr_username = ""
            settings.tr_password = ""
            client = TransmissionClient()
            self.assertIsNone(client.auth)
            self.assertTrue(client.base_url.endswith("/transmission/rpc"))
        finally:
            settings.tr_base_url, settings.tr_username, settings.tr_password = previous


class RuntimeSettingsPartialUpdateTests(IsolatedAppTestCase):
    async def test_partial_update_keeps_omitted_values_and_nulls_preserve_existing(self) -> None:
        with TestClient(app) as client:
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
        with TestClient(app) as client:
            baseline = client.get("/api/settings").json()
            bad = client.put("/api/settings", json={"tr_base_url": "not a url"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(baseline["tr_base_url"], "")


class ConnectionEndpointTests(IsolatedAppTestCase):
    """2-19：补齐剩余零覆盖路由的最小行为断言。"""

    async def test_connection_endpoint(self) -> None:
        with TestClient(app) as client:
            connection = client.get("/api/connection")
            self.assertEqual(connection.status_code, 200)
            self.assertIn("providers", connection.json())


class SettingsSaveTests(IsolatedAppTestCase):
    async def test_first_save_without_transmission_and_explicit_username_clear(self) -> None:
        settings.tr_username = ""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put("/api/settings", json={"tr_username": None, "tmdb_language": "en-US"})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["tr_username_configured"])
            self.assertEqual(settings.tmdb_language, "en-US")
            await client.put("/api/settings", json={"tr_username": "audit-user"})
            response = await client.put("/api/settings", json={"tr_username": None, "clear_tr_username": True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(settings.tr_username, "")

    async def test_proxy_toggle_round_trip_and_partial_updates(self) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for enabled in (True, False):
                response = await client.put("/api/settings", json={"tmdb_proxy_enabled": enabled})
                self.assertEqual(response.status_code, 200)
                self.assertIs(response.json().get("tmdb_proxy_enabled"), enabled)
                await client.put("/api/settings", json={"tmdb_language": "zh-CN"})
                public = (await client.get("/api/settings")).json()
                self.assertIs(public.get("tmdb_proxy_enabled"), enabled)

    async def test_save_settings_allows_private_hosts_when_access_token_configured(self) -> None:
        """开启访问令牌时，MoviePilot、Emby、Transmission、AI 与代理等受信任下游服务仍允许配置局域网私有地址。"""
        strong_token = "audit-token-with-sufficient-entropy-32chars!!"
        payload = {
            "mp_base_url": "http://192.168.31.100:3000",
            "emby_base_url": "http://192.168.31.100:8096",
            "tr_base_url": "http://192.168.31.100:9191",
            "ai_base_url": "http://192.168.31.100:11434",
            "outbound_proxy_url": "http://192.168.31.100:7890",
        }
        with patch.dict(os.environ, {"AUTOLIST_ACCESS_TOKEN": strong_token, "AUTOLIST_REQUIRE_STRONG_TOKEN": "false"}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.put(
                    "/api/settings", json=payload, headers={"Authorization": f"Bearer {strong_token}"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["mp_base_url"], "http://192.168.31.100:3000")
                self.assertEqual(response.json()["emby_base_url"], "http://192.168.31.100:8096")
                self.assertEqual(response.json()["tr_base_url"], "http://192.168.31.100:9191")
                self.assertEqual(response.json()["ai_base_url"], "http://192.168.31.100:11434")
                self.assertEqual(response.json()["outbound_proxy_url"], "http://192.168.31.100:7890")

    async def test_browser_save_preserves_blank_credentials_and_proxy(self) -> None:
        """新界面的真实组装逻辑：密钥与用户名留空时保留已保存的值，开关状态照常保存。"""
        settings.tr_username = "audit-user"
        settings.tr_password = "audit-password"
        settings.tmdb_api_key = "audit-tmdb-value"
        settings.outbound_proxy_url = "http://proxy.example:7890"
        settings.tmdb_proxy_enabled = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            public = (await client.get("/api/settings")).json()
            form = {
                "tr_base_url": public["tr_base_url"], "tr_username": "", "tr_password": "",
                "tmdb_api_key": "", "tmdb_language": "", "tmdb_proxy_enabled": public["tmdb_proxy_enabled"],
                "outbound_proxy_url": "",
            }
            fields = list(form)
            payload = run_payload_module(f"m.buildSettingsPayload({json.dumps(form)}, {json.dumps(fields)}, [])")
            self.assertIsNone(payload["tr_password"])
            self.assertIsNone(payload["tr_username"])
            self.assertNotIn("clear_tr_password", payload)
            response = await client.put("/api/settings", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["tmdb_proxy_enabled"])
            self.assertEqual(response.json()["tr_password"], "")
            self.assertEqual(response.json()["tmdb_api_key"], "")
            self.assertTrue(response.json()["tr_username_configured"])
        # 校验持久化后的配置，而不只是响应或内存状态。
        settings.tr_username = settings.tr_password = settings.tmdb_api_key = settings.outbound_proxy_url = ""
        settings.tmdb_proxy_enabled = False
        load_runtime_settings()
        self.assertEqual(settings.tr_username, "audit-user")
        self.assertEqual(settings.tr_password, "audit-password")
        self.assertEqual(settings.tmdb_api_key, "audit-tmdb-value")
        self.assertEqual(settings.outbound_proxy_url, "http://proxy.example:7890")
        self.assertTrue(settings.tmdb_proxy_enabled)

    async def test_browser_clear_marks_only_blank_clearable_fields(self) -> None:
        settings.tmdb_api_key = "audit-tmdb-value"
        settings.tr_password = "audit-password"
        form = {"tmdb_api_key": "", "tr_password": "typed-new-value"}
        payload = run_payload_module(
            f"m.buildSettingsPayload({json.dumps(form)}, {json.dumps(list(form))}, ['tmdb_api_key', 'tr_password'])"
        )
        self.assertEqual(payload, {"tmdb_api_key": None, "clear_tmdb_api_key": True, "tr_password": "typed-new-value"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put("/api/settings", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(settings.tmdb_api_key, "")
        self.assertEqual(settings.tr_password, "typed-new-value")

    def test_clearable_fields_are_known_to_server(self) -> None:
        clearable = run_payload_module("m.CLEARABLE_FIELDS")
        self.assertTrue(set(clearable) <= set(RUNTIME_SETTING_CLEAR_FIELDS), set(clearable) - set(RUNTIME_SETTING_CLEAR_FIELDS))
        secrets = run_payload_module("m.SECRET_FIELDS")
        self.assertTrue(set(secrets) <= set(clearable))


class ConnectionSelectionTests(IsolatedAppTestCase):
    async def test_single_provider_does_not_contact_other_services(self):
        with patch("app.api.system.TMDBClient.check", new=AsyncMock(return_value={"ok": False, "configured": False})) as tmdb, patch("app.api.system.MoviePilotClient.check", new=AsyncMock()) as mp:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                result = await client.post("/api/settings/test?provider=tmdb", json={})
                invalid = await client.post("/api/settings/test?provider=unknown", json={})
            self.assertEqual(result.json(), {"tmdb": {"ok": False, "configured": False}})
            self.assertEqual(invalid.status_code, 422)
            tmdb.assert_awaited_once()
            mp.assert_not_awaited()


class SettingsKeepTests(IsolatedAppTestCase):
    async def test_null_cookiecloud_url_keeps_saved_address(self) -> None:
        import httpx

        from app.main import app

        settings.cookiecloud_url = "http://cc.example:8088/cookiecloud"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            kept = await client.put("/api/settings", json={"cookiecloud_url": None, "tmdb_proxy_enabled": True})
            self.assertEqual(kept.status_code, 200, kept.text)
            self.assertEqual(settings.cookiecloud_url, "http://cc.example:8088/cookiecloud")
            cleared = await client.put("/api/settings", json={"clear_cookiecloud_url": True})
            self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(settings.cookiecloud_url, "")
