from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.clients import MTeamClient, NexusPHPClient


def response(url: str, *, text: str = "", json_body: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", url)
    if json_body is not None:
        return httpx.Response(200, json=json_body, request=request)
    return httpx.Response(200, text=text, request=request)


class TrackerAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mteam_missing_code_with_error_message_is_rejected(self) -> None:
        upstream = response(
            "https://tracker.example/api/torrent/search",
            json_body={"message": "INVALID_TOKEN", "data": {"data": []}},
        )
        site = {
            "name": "M-Team",
            "base_url": "https://tracker.example",
            "api_key": "configured-test-key",
            "timeout_seconds": 5,
        }
        with patch("app.clients.safe_request", new=AsyncMock(return_value=upstream)):
            with self.assertRaisesRegex(RuntimeError, "INVALID_TOKEN"):
                await MTeamClient().search(site, "AutoListConnectionProbe")

    async def test_nexus_login_page_is_not_reported_as_empty_search(self) -> None:
        upstream = response(
            "https://tracker.example/login.php",
            text="""
                <html><body>
                  <form action="takelogin.php" method="post">
                    <input name="username">
                    <input name="password" type="password">
                  </form>
                </body></html>
            """,
        )
        site = {
            "name": "Nexus",
            "base_url": "https://tracker.example",
            "cookie": "expired=test",
            "timeout_seconds": 5,
        }
        with patch("app.clients.safe_request", new=AsyncMock(return_value=upstream)):
            with self.assertRaisesRegex(RuntimeError, "Cookie 已失效"):
                await NexusPHPClient().search(site, "AutoListConnectionProbe")


if __name__ == "__main__":
    unittest.main()
