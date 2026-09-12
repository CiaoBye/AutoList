"""Settings API and the browser's actual save payload must share one contract."""

import json
from pathlib import Path
import subprocess

import httpx

from app.config import load_runtime_settings, settings
from app.main import app
from tests.support import IsolatedAppTestCase


class SettingsSaveTests(IsolatedAppTestCase):
    async def test_browser_save_preserves_blank_credentials_and_proxy(self) -> None:
        settings.tr_username = "audit-user"
        settings.tr_password = "audit-password"
        settings.tmdb_api_key = "audit-tmdb-value"
        settings.tmdb_proxy_enabled = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            public = (await client.get("/api/settings")).json()
            # Execute the production payload builder against empty settings inputs.
            script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const runtime = JSON.parse(process.argv[1]);
const context = {
  $: selector => ({value: selector === '#settings-timeout' ? '30' : '',
    checked: selector === '#settings-tmdb-proxy' && Boolean(runtime.tmdb_proxy_enabled)}),
  document: {querySelector: () => ({checked: false})},
  api: async (path, options) => process.stdout.write(options.body),
  loadSettings: async () => {}, testSettings: async () => {}, loadConnection: async () => {},
};
for (const name of ['secretValue', 'clearSettingSelected', 'saveSettings']) {
  const start = source.indexOf((name === 'saveSettings' ? 'async ' : '') + 'function ' + name + '(');
  const end = source.indexOf('\n}', start) + 2;
  vm.runInNewContext(source.slice(start, end), context);
}
context.saveSettings().catch(error => { console.error(error); process.exitCode = 1; });
"""
            payload = json.loads(subprocess.check_output(
                ["node", "-e", script, json.dumps(public)],
                cwd=Path(__file__).resolve().parent.parent, text=True,
            ))
            response = await client.put("/api/settings", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["tmdb_proxy_enabled"])
            self.assertEqual(response.json()["tr_username"], "")
            self.assertEqual(response.json()["tr_password"], "")
            self.assertEqual(response.json()["tmdb_api_key"], "")
            self.assertTrue(response.json()["tr_username_configured"])
        # Verify persisted settings, not just the response or in-memory state.
        settings.tr_username = settings.tr_password = settings.tmdb_api_key = ""
        settings.tmdb_proxy_enabled = False
        load_runtime_settings()
        self.assertEqual(settings.tr_username, "audit-user")
        self.assertEqual(settings.tr_password, "audit-password")
        self.assertEqual(settings.tmdb_api_key, "audit-tmdb-value")
        self.assertTrue(settings.tmdb_proxy_enabled)

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
