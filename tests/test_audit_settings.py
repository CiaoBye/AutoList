"""Settings API and the browser's actual save payload must share one contract."""

import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import httpx

from app.api.system import RUNTIME_SETTING_CLEAR_FIELDS
from app.config import load_runtime_settings, settings
from app.main import app
from tests.support import IsolatedAppTestCase

ROOT = Path(__file__).resolve().parent.parent


def run_payload_module(expression: str) -> object:
    """用 Node 直接执行 frontend/src/settingsPayload.ts，返回表达式的 JSON 结果。"""
    script = f"import * as m from './frontend/src/settingsPayload.ts'; console.log(JSON.stringify({expression}));"
    output = subprocess.check_output(
        ["node", "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", script],
        cwd=ROOT, text=True,
    )
    return json.loads(output)


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
            "mp_base_url": "http://192.0.2.10:3000",
            "emby_base_url": "http://192.0.2.10:8096",
            "tr_base_url": "http://192.0.2.10:9191",
            "ai_base_url": "http://192.0.2.10:11434",
            "outbound_proxy_url": "http://192.0.2.10:7890",
        }
        with patch.dict(os.environ, {"AUTOLIST_ACCESS_TOKEN": strong_token, "AUTOLIST_REQUIRE_STRONG_TOKEN": "false"}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.put(
                    "/api/settings", json=payload, headers={"Authorization": f"Bearer {strong_token}"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["mp_base_url"], "http://192.0.2.10:3000")
                self.assertEqual(response.json()["emby_base_url"], "http://192.0.2.10:8096")
                self.assertEqual(response.json()["tr_base_url"], "http://192.0.2.10:9191")
                self.assertEqual(response.json()["ai_base_url"], "http://192.0.2.10:11434")
                self.assertEqual(response.json()["outbound_proxy_url"], "http://192.0.2.10:7890")

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
