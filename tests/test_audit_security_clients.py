"""Regression coverage for settings-adjacent client and credential handling."""

import os
import unittest
from unittest.mock import patch

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
