from unittest.mock import AsyncMock, patch

import httpx

from app.main import app
from tests.support import IsolatedAppTestCase


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
