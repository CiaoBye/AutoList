import asyncio
import base64
import concurrent.futures
import gzip
import hashlib
import json
import os
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from io import BytesIO
from unittest.mock import AsyncMock, patch

from defusedxml import ElementTree
from defusedxml.common import EntitiesForbidden
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi import HTTPException
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.compat import main  # noqa: E402 (审计 2-12：测试兼容层) # type: ignore[import-not-found]
from app.healthcheck import validate_scheduler_health
from app.api import playlists as playlist_routes
from app.api import search as search_routes
from app.api import sites as site_routes
from app.api import system as system_routes
from app.candidate_policy import DEFAULT_POLICY, release_group_catalog
from app.clients import MoviePilotClient, TransmissionClient
from app.database import config_values
from app.services.imports import MAX_XLSX_ENTRIES
from app.state import MAX_RUNNING_AUTOMATION_TASKS, MAX_RUNNING_RECOGNITION_TASKS, running_automation_tasks, running_recognition_tasks
from app.config import save_runtime_settings, settings
from app.cookiecloud import cookie_for_host
from app.database import SCHEMA_VERSION, cleanup_old_data, connect, initialize
from app.list_sources import validate_source_url
from app.security import sanitize_sensitive_text
from app.services.automation import run_playlist_automation
from app.services.history import clear_download_history, projected_download_history
from app.services.sites import apply_cookie_groups, refresh_stale_site_account_stats
from app.services.sites import test_site_config as _test_site_config  # noqa: F401 —— 别名导入避免 pytest 将其收集为测试用例
from app.util import secret_free, to_int


class SecurityTests(unittest.TestCase):
    def test_moviepilot_gateway_has_no_search_compatibility_methods(self) -> None:
        client = MoviePilotClient()
        for removed in (
            "search_title", "search_media", "add_download", "sites", "site_statistics",
            "site_user_data", "refresh_site_user_data", "update_site_cookie", "site_icon",
            "custom_release_groups",
        ):
            self.assertFalse(hasattr(client, removed))
        self.assertTrue(callable(client.check))
        self.assertTrue(callable(client.download))
        route_paths = {route.path for route in main.app.routes}
        self.assertNotIn("/api/sites/moviepilot-enhancements", route_paths)
        self.assertNotIn("/api/config/release-groups/import-moviepilot", route_paths)
        self.assertNotIn("cookiecloud_forward_moviepilot", settings.public_values())

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

    def test_only_raster_magic_bytes_are_accepted_for_site_icons(self) -> None:
        self.assertEqual(main.raster_image_media_type(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertIsNone(main.raster_image_media_type(b"<svg onload='alert(1)'></svg>"))

    def test_site_icon_fallback_uses_site_name_not_url_scheme(self) -> None:
        fallback = site_routes.site_icon_fallback("朱雀", "https://zhuque.in").decode("utf-8")
        self.assertIn(">朱雀</text>", fallback)
        self.assertNotIn(">HT</text>", fallback)
        self.assertEqual(site_routes.SITE_ICON_ENDPOINT_VERSION, 2)

        unsafe = site_routes.site_icon_fallback('"<&', "https://example.test").decode("utf-8")
        self.assertIn("aria-label=\"&quot;&lt;\"", unsafe)
        self.assertNotIn('aria-label=""', unsafe)

    def test_c_archive_ui_keeps_required_business_hooks_unique(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
        for marker in ("screening-dashboard", "site-constellation-workspace", "site-live-map", "site-inspector-panel"):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)
        for element_id in (
            "dashboard-new-count", "dashboard-pending", "metric-playlist", "dashboard-task-pill",
            "dashboard-task-title", "dashboard-task-copy", "dashboard-task-bar", "metric-candidates",
            "metric-search", "metric-search-state", "metric-items", "metric-items-progress",
            "dashboard-recognized", "dashboard-in-library", "dashboard-in-library-progress",
            "dashboard-history-count", "metric-cart", "metric-cart-size", "dashboard-library-bar",
        ):
            with self.subTest(element_id=element_id):
                self.assertEqual(html.count(f'id="{element_id}"'), 1)
        self.assertNotIn('class="screening-recent"', html)
        self.assertNotIn('class="screening-activity"', html)
        self.assertNotIn('id="settings-random-posters"', html)

    def test_theme_ui_keeps_three_presets_and_shared_scene_hooks(self) -> None:
        static_dir = Path(__file__).resolve().parents[1] / "app" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        theme_init = (static_dir / "js" / "theme-init.js").read_text(encoding="utf-8")
        theme_css = (static_dir / "theme.css").read_text(encoding="utf-8")
        self.assertIn('<html lang="zh-CN" data-theme="archive"', html)
        self.assertIn('src="/assets/js/theme-init.js?v=1.21.0"', html)
        self.assertIn('href="/assets/theme.css?v=1.21.0"', html)
        for theme in ("archive", "cinema", "ledger"):
            with self.subTest(theme=theme):
                self.assertIn(f'{theme}: Object.freeze', theme_init)
                self.assertIn(f'data-theme-option="{theme}"', html)
                self.assertIn(f'html[data-theme="{theme}"]', theme_css)
        for page in ("dashboard", "playlists", "search", "cart", "rules", "sites", "history", "logs"):
            with self.subTest(page=page):
                self.assertIn(f'data-page="{page}"', html)
                self.assertIn(f'body[data-page="{page}"]', theme_css)
        self.assertIn('id="settings-scene-mode"', html)
        self.assertIn('id="settings-reduced-motion"', html)
        self.assertNotIn("theme-copy", html)
        for palette_copy in ("浅色 · 纸白、青绿与琥珀", "深色 · 深青、冰蓝与琥珀", "纸张 · 米白、铁锈与深青"):
            with self.subTest(palette_copy=palette_copy):
                self.assertIn(palette_copy, html)
        for stale_copy in ("待放映资源", "放映日志", "场次与放映队列", "数据与编号优先"):
            with self.subTest(stale_copy=stale_copy):
                self.assertNotIn(stale_copy, html)
        self.assertIn("来源档案室", (static_dir / "app.js").read_text(encoding="utf-8"))
        for label in ("电影藏馆", "馆藏片单", "来源检索", "待入馆", "入馆标准", "入馆动态", "来源网络", "操作日志"):
            with self.subTest(label=label):
                self.assertIn(label, html)
        self.assertIn("滚轮或双指缩放", html)
        site_map_js = (static_dir / "js" / "site-map.js").read_text(encoding="utf-8")
        app_js = (static_dir / "app.js").read_text(encoding="utf-8")
        # 3-10 拆分后：地图计算层在 js/site-map.js，app.js 通过 ES 模块导入。
        self.assertIn('data-site-map-zoom="in"', app_js)
        self.assertIn("siteMapViewportStorageKey", site_map_js)
        self.assertIn("site-map-canvas", app_js)
        self.assertIn('id="reset-site-layout"', html)
        self.assertIn('id="toggle-site-orientation"', html)
        self.assertIn("siteMapOrientationStorageKey", site_map_js)
        self.assertIn('class="nav-item-label"', html)
        self.assertIn("siteNodePositionStorageKey", site_map_js)
        self.assertNotIn('document.documentElement.dataset.theme = "light"', (static_dir / "app.js").read_text(encoding="utf-8"))
        self.assertIn('canvas.style.transform = `translate3d(', site_map_js)
        self.assertIn('export function siteMapCopy()', site_map_js)
        self.assertIn('label.textContent = "界面主题"', app_js)
        self.assertNotIn("theme-copy", theme_css)

    def test_theme_variants_share_layout_and_keep_palette_contracts(self) -> None:
        static_dir = Path(__file__).resolve().parents[1] / "app" / "static"
        theme_css = (static_dir / "theme.css").read_text(encoding="utf-8")
        app_js = (static_dir / "app.js").read_text(encoding="utf-8")
        for marker in (
            "--cinema-primary-bg",
            'html[data-theme="cinema"] .filter-chip.active',
            "1.18 theme contract",
            'html[data-theme] body[data-page="dashboard"] .screening-mission-grid',
            'html[data-theme] body[data-page="search"] .search-console',
            'html[data-theme] body[data-page="sites"] .site-live-map',
            'html[data-theme] .site-star-node.selected .site-node-core',
            '@media (max-width: 680px)',
            "@media (min-width: 901px)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, theme_css)
        for legacy_layout in (
            'html[data-theme="ledger"] body[data-page="sites"] .site-linear-list-items { grid-template-columns: 1fr;',
            'html[data-theme="cinema"] body[data-page="search"] .search-console { display: grid;',
            'html[data-theme="ledger"] .screening-hero { min-height:',
            'html[data-theme="ledger"] body[data-page="dashboard"] .screening-feature { min-height:',
        ):
            with self.subTest(legacy_layout=legacy_layout):
                self.assertNotIn(legacy_layout, theme_css)
        self.assertIn('class="candidate-index"', app_js)

    def test_inline_svg_icons_have_explicit_size_and_safe_paint_contract(self) -> None:
        static_dir = Path(__file__).resolve().parents[1] / "app" / "static"
        style_css = (static_dir / "style.css").read_text(encoding="utf-8")
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn(".inline-icon", style_css)
        self.assertIn(".rule-summary > i > svg", style_css)
        self.assertIn(".history-status .inline-icon", style_css)
        self.assertIn('<div class="rule-summary"', html)
        self.assertNotIn('<svg class="theme-copy', html)

    def test_playlist_source_url_rejects_embedded_secrets(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能包含"):
            validate_source_url("https://letterboxd.com/user/list/example/?token=secret")

    def test_service_urls_reject_secret_query_parameters(self) -> None:
        for query in ("token=secret", "passkey=secret", "api_key=secret", "key=secret"):
            with self.subTest(query=query):
                with self.assertRaises(HTTPException):
                    main.validated_base_url(f"https://service.example/api?{query}", "服务地址", False)

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

    def test_cookiecloud_gzip_expansion_is_bounded(self) -> None:
        payload = gzip.compress(b"x" * 2048)
        with self.assertRaises(HTTPException) as raised:
            main.decode_cookiecloud_body(payload, "gzip", limit=1024)
        self.assertEqual(raised.exception.status_code, 413)

    def test_empty_xlsx_returns_a_readable_validation_error(self) -> None:
        workbook = Workbook()
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        encoded = base64.b64encode(output.getvalue()).decode()
        with self.assertRaises(HTTPException) as raised:
            main.parse_xlsx(encoded)
        self.assertEqual(raised.exception.status_code, 422)

    def test_cookiecloud_uuid_must_match_configured_key(self) -> None:
        previous = settings.cookiecloud_key
        settings.cookiecloud_key = "configured-key"
        try:
            rejection = main.require_configured_cookiecloud_uuid("other-key")
            self.assertEqual(rejection[0], 403)
            self.assertIsNone(main.require_configured_cookiecloud_uuid("configured-key"))
        finally:
            settings.cookiecloud_key = previous

    def test_cookiecloud_does_not_apply_subdomain_cookie_to_parent_site(self) -> None:
        groups = {
            "example.org": "parent=1",
            "private.example.org": "child=1",
        }
        self.assertEqual(cookie_for_host(groups, "pt.example.org"), ("example.org", "parent=1"))
        self.assertEqual(cookie_for_host(groups, "private.example.org"), ("private.example.org", "child=1"))
        self.assertIsNone(cookie_for_host({"private.example.org": "child=1"}, "example.org"))

    def test_cookiecloud_rejects_write_without_configured_key(self) -> None:
        previous = settings.cookiecloud_key
        settings.cookiecloud_key = ""
        try:
            rejection = main.require_configured_cookiecloud_uuid("any-key")
            self.assertEqual(rejection[0], 503)
        finally:
            settings.cookiecloud_key = previous

    def test_access_token_helpers_use_constant_time_compare(self) -> None:
        from app.security import extract_access_token, token_matches

        self.assertTrue(token_matches("secret-token", "secret-token"))
        self.assertFalse(token_matches("secret-token", "other-token"))
        self.assertFalse(token_matches("", "secret-token"))
        self.assertEqual(extract_access_token("Bearer abc123", None), "abc123")
        self.assertEqual(extract_access_token(None, " header-token "), "header-token")

    def test_search_task_capacity_is_enforced(self) -> None:
        previous = dict(main.running_tasks)
        main.running_tasks.clear()

        class Alive:
            def done(self) -> bool:
                return False

        try:
            for index in range(main.MAX_RUNNING_SEARCH_TASKS):
                main.running_tasks[index] = Alive()  # type: ignore[assignment]
            rejection = main.enforce_search_task_capacity()
            self.assertIsNotNone(rejection)
            self.assertIn("搜索任务", rejection or "")
        finally:
            main.running_tasks.clear()
            main.running_tasks.update(previous)

    def test_access_token_middleware_protects_api_but_keeps_health_open(self) -> None:
        previous_token = os.environ.get("AUTOLIST_ACCESS_TOKEN")
        previous_strict = os.environ.get("AUTOLIST_REQUIRE_STRONG_TOKEN")
        previous_data_dir = settings.data_dir
        temp = tempfile.TemporaryDirectory()
        os.environ["AUTOLIST_ACCESS_TOKEN"] = "unit-test-token"
        os.environ["AUTOLIST_REQUIRE_STRONG_TOKEN"] = "false"
        settings.data_dir = temp.name
        try:
            with TestClient(main.app) as client:
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

    def test_shell_resources_revalidate_after_deploy(self) -> None:
        previous_data_dir = settings.data_dir
        with tempfile.TemporaryDirectory() as temp:
            settings.data_dir = temp
            try:
                with TestClient(main.app) as client:
                    index = client.get("/")
                    favicon = client.get("/favicon.ico")
            finally:
                settings.data_dir = previous_data_dir
        self.assertEqual(index.status_code, 200)
        self.assertEqual(favicon.status_code, 200)
        self.assertEqual(index.headers.get("cache-control"), "no-cache, must-revalidate")
        self.assertEqual(favicon.headers.get("cache-control"), "no-cache, must-revalidate")

    def test_healthcheck_rejects_stopped_or_stale_scheduler_only(self) -> None:
        for status in ("starting", "degraded", "ok"):
            with self.subTest(status=status):
                validate_scheduler_health({
                    "scheduler": {"status": status, "last_heartbeat_age_seconds": 10},
                })
        for payload in (
            {"scheduler": {"status": "stopped", "last_heartbeat_age_seconds": None}},
            {"scheduler": {"status": "stale", "last_heartbeat_age_seconds": 181}},
            {"scheduler": {"status": "degraded", "last_heartbeat_age_seconds": 181}},
        ):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                validate_scheduler_health(payload)


class DatabaseAndApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous_data_dir = settings.data_dir
        self.previous_dashboard_random_posters = settings.dashboard_random_posters
        settings.data_dir = self.temp.name
        settings.dashboard_random_posters = False
        main.raw_candidates.clear()
        main.poster_cache.clear()
        from app.services import search as search_module
        search_module._transmission_snapshot_cache.clear()
        initialize()
        with connect() as conn:
            playlist_id = conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)", ("测试片单", 1, main.utc_now()),
            ).lastrowid
            self.playlist_id = to_int(playlist_id)
            conn.executemany(
                """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,library_state)
                   VALUES(?,?,?,?,?,?,?)""",
                [
                    (self.playlist_id, index, f"tt{index:07d}", f"Movie {index}", 2000 + index % 20, f"电影 {index}", "in_library" if index % 2 else "unknown")
                    for index in range(1, 251)
                ],
            )

    async def asyncTearDown(self) -> None:
        main.raw_candidates.clear()
        main.poster_cache.clear()
        from app.services import search as search_module
        search_module._transmission_snapshot_cache.clear()
        settings.data_dir = self.previous_data_dir
        settings.dashboard_random_posters = self.previous_dashboard_random_posters
        self.temp.cleanup()

    async def test_database_uses_wal_and_busy_timeout(self) -> None:
        with connect() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlist_items)")}
        self.assertTrue({"emby_item_id", "emby_image_tag"}.issubset(columns))
        with connect() as conn:
            playlist_columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlists)")}
            task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(search_tasks)")}
            download_columns = {row["name"] for row in conn.execute("PRAGMA table_info(download_history)")}
            site_columns = {row["name"] for row in conn.execute("PRAGMA table_info(pt_sites)")}
        self.assertTrue({"automation_enabled", "sync_enabled", "next_sync_at"}.issubset(playlist_columns))
        self.assertTrue({"parent_task_id", "trigger", "site_ids_json", "item_ids_json", "pair_scope_json"}.issubset(task_columns))
        self.assertTrue({"playlist_item_id", "submission_hash", "playlist_item_snapshot_json", "resource_key"}.issubset(download_columns))
        self.assertTrue({
            "account_uploaded", "account_downloaded", "account_ratio",
            "account_stats_checked_at", "account_stats_error", "last_duration_ms",
        }.issubset(site_columns))

    async def test_initialize_migrates_history_resource_key_and_rejects_future_schema(self) -> None:
        item_id: int
        with connect() as conn:
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid)
            item_id = to_int(conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0])
            conn.execute(
                """INSERT INTO candidates(
                     id,task_id,playlist_item_id,candidate_index,title,site_name,size,ranking,metadata_json,resource_key,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("migration-candidate", task_id, item_id, 0, "Movie.2020.1080p", "迁移站", 1024, 1, "{}", "movie-key:16", main.utc_now()),
            )
            conn.execute("DROP TABLE download_history")
            conn.execute(
                """CREATE TABLE download_history(
                     id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT, playlist_item_id INTEGER,
                     playlist_item_snapshot_json TEXT, title TEXT NOT NULL, torrent_name TEXT NOT NULL,
                     site_name TEXT, submission_hash TEXT, success INTEGER NOT NULL, message TEXT, created_at TEXT NOT NULL
                   )""",
            )
            conn.execute(
                """INSERT INTO download_history(
                     candidate_id,playlist_item_id,title,torrent_name,site_name,success,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                ("migration-candidate", item_id, "Movie 1", "Movie.2020.1080p", "迁移站", 1, main.utc_now()),
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
        initialize()
        with connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(download_history)")}
            history = conn.execute("SELECT resource_key FROM download_history").fetchone()
        self.assertIn("resource_key", columns)
        self.assertEqual(history["resource_key"], "movie-key:16")

        with connect() as conn:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        with self.assertRaises(RuntimeError):
            initialize()
        with connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 1)

    async def test_runtime_settings_clear_protocol_invalidates_connection_cache(self) -> None:
        previous_values = {
            "tmdb_api_key": settings.tmdb_api_key,
            "outbound_proxy_url": settings.outbound_proxy_url,
        }
        settings.tmdb_api_key = "tmdb-secret"
        settings.outbound_proxy_url = "http://192.168.31.99:1080"
        system_routes._connection_cache = (0.0, {"providers": {"stale": True}})
        try:
            with TestClient(main.app) as client:
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
        self.assertEqual(result, {"fresh": True})
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

    async def test_site_connection_records_slow_state_and_duration(self) -> None:
        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,created_at) VALUES(?,?,?,?)",
                ("慢速站点", "nexusphp", "https://slow.example", main.utc_now()),
            ).lastrowid)
            site = dict(conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone())
        with patch("app.services.sites.time.monotonic", side_effect=[0, 4]), \
             patch("app.services.sites.NexusPHPClient.check", new=AsyncMock(return_value={"ok": True, "message": "连接正常"})):
            result = await _test_site_config(site)
        self.assertEqual(result["status"], "slow")
        self.assertTrue(result["ok"])
        self.assertEqual(result["duration_ms"], 4000)
        with connect() as conn:
            stored = conn.execute("SELECT last_status,last_duration_ms FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        self.assertEqual((stored["last_status"], stored["last_duration_ms"]), ("slow", 4000))

    async def test_site_connection_rejects_explicit_failed_result(self) -> None:
        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,created_at) VALUES(?,?,?,?)",
                ("失败响应站点", "nexusphp", "https://failed.example", main.utc_now()),
            ).lastrowid)
            site = dict(conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone())
        with patch(
            "app.services.sites.NexusPHPClient.check",
            new=AsyncMock(return_value={"ok": False, "message": "认证失败"}),
        ):
            result = await _test_site_config(site)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "认证失败")

    async def test_local_site_list_does_not_call_moviepilot(self) -> None:
        with connect() as conn:
            site_id = conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,enabled,search_enabled,created_at) VALUES(?,?,?,?,?,?,?)",
                ("独立站点", "nexusphp", "https://tracker.example", "session=secret", 1, 1, main.utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid
            conn.execute(
                """INSERT INTO search_attempts(
                       task_id,playlist_item_id,site_id,site_name,status,result_count,duration_ms,created_at,finished_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (task_id, item_id, site_id, "独立站点", "success", 3, 240, main.utc_now(), main.utc_now()),
            )
        rows = await site_routes.sites()
        self.assertEqual(rows[-1]["name"], "独立站点")
        self.assertTrue(rows[-1]["cookie_configured"])
        self.assertEqual(rows[-1]["cookie"], "")
        self.assertEqual(rows[-1]["local_stats"]["success_rate"], 100.0)
        self.assertEqual(rows[-1]["local_stats"]["average_ms"], 240)
        self.assertEqual(rows[-1]["local_stats"]["result_count"], 3)
        self.assertNotIn("mp_enhancement", rows[-1])

    async def test_add_site_keeps_url_validation_available_after_route_split(self) -> None:
        payload = site_routes.SitePayload(
            name="新增站点",
            base_url="https://tracker.example",
            cookie="session=test",
            limit_interval=1,
            limit_count=1,
            enabled=True,
            search_enabled=True,
        )
        result = await site_routes.add_site(payload)
        self.assertEqual(result["name"], "新增站点")
        with connect() as conn:
            stored = conn.execute("SELECT base_url FROM pt_sites WHERE id=?", (result["id"],)).fetchone()
        self.assertEqual(stored["base_url"], "https://tracker.example")

    async def test_cookiecloud_updates_local_site_without_moviepilot(self) -> None:
        with connect() as conn:
            site_id = conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,created_at) VALUES(?,?,?,?,?)",
                ("Cookie 站点", "nexusphp", "https://tracker.example", "", main.utc_now()),
            ).lastrowid
        with patch.object(site_routes, "stored_cookiecloud_payload", return_value={
            "cookie_data": {
                ".tracker.example": [
                    {"domain": ".tracker.example", "name": "session", "value": "fresh-cookie"},
                ],
            },
        }):
            result = await site_routes.sync_sites_from_cookiecloud()
        with connect() as conn:
            cookie = conn.execute("SELECT cookie FROM pt_sites WHERE id=?", (site_id,)).fetchone()[0]
        self.assertEqual(cookie, "session=fresh-cookie")
        self.assertEqual(result["updated"], 1)

    async def test_single_site_cookie_refresh_uses_autolist_cookiecloud(self) -> None:
        with connect() as conn:
            site_id = conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,user_agent,created_at) VALUES(?,?,?,?,?,?)",
                ("单站刷新", "nexusphp", "https://tracker.example", "old=1", "Local UA", main.utc_now()),
            ).lastrowid
        with patch.object(site_routes, "stored_cookiecloud_payload", return_value={
            "cookie_data": {
                ".tracker.example": [
                    {"domain": ".tracker.example", "name": "session", "value": "new-cookie"},
                ],
            },
        }):
            result = await site_routes.refresh_site_cookie(to_int(site_id))
        with connect() as conn:
            row = conn.execute("SELECT cookie,user_agent FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        self.assertTrue(result["ok"])
        self.assertEqual(row["cookie"], "session=new-cookie")
        self.assertEqual(row["user_agent"], "Local UA")

    async def test_cookie_groups_apply_automatically_without_overwriting_user_agent(self) -> None:
        with connect() as conn:
            site_id = to_int(conn.execute(
                """INSERT INTO pt_sites(
                       name,adapter,base_url,cookie,user_agent,account_stats_checked_at,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                ("自动 Cookie", "nexusphp", "https://pt.example.org", "old=1", "Browser UA", main.utc_now(), main.utc_now()),
            ).lastrowid)
        result = apply_cookie_groups({"example.org": "session=fresh"})
        with connect() as conn:
            row = conn.execute(
                "SELECT cookie,user_agent,account_stats_checked_at FROM pt_sites WHERE id=?", (site_id,),
            ).fetchone()
        self.assertEqual(result["updated"], ["自动 Cookie"])
        self.assertEqual(row["cookie"], "session=fresh")
        self.assertEqual(row["user_agent"], "Browser UA")
        self.assertIsNone(row["account_stats_checked_at"])

    async def test_cookiecloud_upload_immediately_updates_matching_site(self) -> None:
        previous_key, previous_password = settings.cookiecloud_key, settings.cookiecloud_password
        settings.cookiecloud_key, settings.cookiecloud_password = "upload-test-key-12", "end-to-end-password"
        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,user_agent,created_at) VALUES(?,?,?,?,?,?)",
                ("上传自动更新", "nexusphp", "https://upload.example", "old=1", "Keep UA", main.utc_now()),
            ).lastrowid)
        plaintext = json.dumps({
            "cookie_data": {
                ".upload.example": [
                    {"domain": ".upload.example", "name": "session", "value": "uploaded-cookie"},
                ],
            },
        }).encode()
        key = hashlib.md5(
            b"upload-test-key-12-end-to-end-password", usedforsecurity=False,
        ).hexdigest()[:16].encode()
        encrypted = base64.b64encode(
            AES.new(key, AES.MODE_CBC, b"\0" * 16).encrypt(pad(plaintext, AES.block_size)),
        ).decode()
        try:
            with TestClient(main.app) as client:
                response = client.post("/cookiecloud/update", json={
                    "uuid": "upload-test-key-12",
                    "encrypted": encrypted,
                    "crypto_type": "aes-128-cbc-fixed",
                })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["updated_sites"], 1)
            with connect() as conn:
                row = conn.execute("SELECT cookie,user_agent FROM pt_sites WHERE id=?", (site_id,)).fetchone()
            self.assertEqual(row["cookie"], "session=uploaded-cookie")
            self.assertEqual(row["user_agent"], "Keep UA")
        finally:
            settings.cookiecloud_key, settings.cookiecloud_password = previous_key, previous_password

    async def test_site_account_statistics_are_cached_between_scheduler_runs(self) -> None:
        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,enabled,created_at) VALUES(?,?,?,?,?,?)",
                ("统计站点", "nexusphp", "https://stats.example", "session=1", 1, main.utc_now()),
            ).lastrowid)
        stats = {"uploaded": 10 * 1024**4, "downloaded": 2 * 1024**4, "ratio": 5.0, "bonus": 123.4, "seeding": 8}
        with patch("app.services.sites.NexusPHPClient.account_stats", new=AsyncMock(return_value=stats)) as mocked:
            first = await refresh_stale_site_account_stats()
            second = await refresh_stale_site_account_stats()
        self.assertTrue(first[0]["ok"])
        self.assertEqual(second, [])
        self.assertEqual(mocked.await_count, 1)
        with connect() as conn:
            row = conn.execute(
                "SELECT account_uploaded,account_downloaded,account_ratio,account_seeding FROM pt_sites WHERE id=?",
                (site_id,),
            ).fetchone()
        self.assertEqual(row["account_uploaded"], stats["uploaded"])
        self.assertEqual(row["account_downloaded"], stats["downloaded"])
        self.assertEqual(row["account_ratio"], 5.0)
        self.assertEqual(row["account_seeding"], 8)

    async def test_playlist_automation_and_sync_settings_are_explicitly_safe(self) -> None:
        automation = await main.configure_playlist_automation(
            self.playlist_id, main.PlaylistAutomationPayload(enabled=True, auto_cart=False, batch_size=25),
        )
        self.assertFalse(automation["auto_download"])
        with self.assertRaises(HTTPException) as raised:
            await main.configure_playlist_automation(
                self.playlist_id, main.PlaylistAutomationPayload(enabled=True, auto_cart=True, batch_size=25),
            )
        self.assertEqual(raised.exception.status_code, 422)

    async def test_nonempty_automation_uses_canonical_titles_without_auto_cart(self) -> None:
        with connect() as conn:
            item = dict(conn.execute(
                "SELECT * FROM playlist_items WHERE playlist_id=? AND rank_no=2", (self.playlist_id,),
            ).fetchone())
            conn.execute(
                """UPDATE playlist_items
                   SET tmdb_id=22,tmdb_title='标准标题',tmdb_original_title='Canonical',tmdb_year=2002,
                       tmdb_imdb_id='tt0000002'
                   WHERE id=?""",
                (item["id"],),
            )
            item.update({
                "tmdb_id": 22, "tmdb_title": "标准标题", "tmdb_original_title": "Canonical",
                "tmdb_year": 2002, "tmdb_imdb_id": "tt0000002",
            })
            now = main.utc_now()
            run_id = to_int(conn.execute(
                """INSERT INTO automation_runs(playlist_id,trigger,status,stage,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (self.playlist_id, "manual", "queued", "queued", now, now),
            ).lastrowid)
        queue = {"items": [item]}
        with patch("app.services.automation.searchable_playlist_items", AsyncMock(return_value=queue)), \
             patch("app.services.automation.library_details", AsyncMock(return_value=("in_library", "emby-1", "tag"))):
            await run_playlist_automation(run_id)
        with connect() as conn:
            run = conn.execute("SELECT status,message FROM automation_runs WHERE id=?", (run_id,)).fetchone()
            cart_count = conn.execute("SELECT COUNT(*) FROM cart_items").fetchone()[0]
        self.assertEqual(run["status"], "completed")
        self.assertIn("候选需人工确认", run["message"])
        self.assertEqual(cart_count, 0)
        with self.assertRaises(HTTPException) as raised:
            await main.configure_playlist_sync(
                self.playlist_id, main.PlaylistSyncPayload(enabled=True, interval_hours=24),
            )
        self.assertEqual(raised.exception.status_code, 422)

    async def test_overview_exposes_only_local_emby_poster_urls(self) -> None:
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET library_state='unknown',emby_item_id=NULL,emby_image_tag=NULL")
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE playlist_items SET library_state='in_library',emby_item_id='abc123',emby_image_tag='tag1' WHERE id=?",
                (item_id,),
            )
        result = await main.overview()
        self.assertEqual(result["recent_items"][0]["poster_url"], f"/api/playlist-items/{item_id}/poster?tag=tag1")
        self.assertNotIn("api_key", result["recent_items"][0]["poster_url"])

    async def test_overview_labels_latest_task_playlist_and_not_in_library_count(self) -> None:
        now = main.utc_now()
        with connect() as conn:
            second_playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("第二片单", 2, now),
            ).lastrowid)
            conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (second_playlist_id, 1, 1, "completed", 1, now, now),
            )
        result = await main.overview()
        self.assertEqual(result["playlist_name"], "测试片单")
        self.assertEqual(result["not_in_library_count"], 125)
        self.assertEqual(result["pending_count"], result["not_in_library_count"])
        self.assertEqual(result["latest_task"]["playlist_id"], second_playlist_id)
        self.assertEqual(result["latest_task"]["playlist_name"], "第二片单")

    async def test_history_projects_source_backed_lifecycle_states(self) -> None:
        with connect() as conn:
            items = {
                to_int(row["rank_no"]): row
                for row in conn.execute(
                    "SELECT id,rank_no FROM playlist_items WHERE playlist_id=? AND rank_no BETWEEN 1 AND 5",
                    (self.playlist_id,),
                ).fetchall()
            }
            conn.execute("UPDATE playlist_items SET library_state='unknown',library_checked_at=NULL WHERE id IN (?,?,?,?)", (items[2]["id"], items[3]["id"], items[4]["id"], items[5]["id"]))
            conn.execute("UPDATE playlist_items SET library_state='in_library' WHERE id=?", (items[1]["id"],))
            conn.execute("UPDATE playlist_items SET library_state='strm' WHERE id=?", (items[5]["id"],))
            conn.executemany(
                """INSERT INTO download_history(
                       playlist_item_id,title,torrent_name,site_name,submission_hash,success,message,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (items[1]["id"], "Movie 1", "Movie 1 2001 1080p", "站点一", "hash-1", 1, None, main.utc_now()),
                    (items[2]["id"], "Movie 2", "Movie 2 2002 1080p", "站点二", "hash-2", 1, None, main.utc_now()),
                    (items[3]["id"], "Movie 3", "Movie 3 2003 1080p", "站点三", "hash-3", 1, None, main.utc_now()),
                    (items[4]["id"], "Movie 4", "Movie 4 2004 1080p", "站点四", "hash-4", 0, "提交失败 passkey=secret", main.utc_now()),
                    (items[5]["id"], "Movie 5", "Movie 5 2005 1080p", "站点五", "hash-5", 1, None, main.utc_now()),
                ],
            )
        torrents = [{
            "id": 22, "name": "Movie 2 2002 1080p", "hashString": "hash-2", "status": 4,
            "percentDone": 0.24, "labels": ["MOVIEPILOT", "站点二"], "downloadDir": "/Media/Raw/Film",
        }]
        with patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=torrents)):
            result = await main.history()
        by_title = {item["title"]: item for item in result}
        self.assertEqual(by_title["Movie 1"]["lifecycle_status"], "organized")
        self.assertEqual(by_title["Movie 1"]["status_label"], "已整理/已入库")
        self.assertEqual(by_title["Movie 1"]["status_source"], "Emby")
        self.assertEqual(by_title["Movie 2"]["lifecycle_status"], "downloading")
        self.assertEqual(by_title["Movie 2"]["status_label"], "下载中")
        self.assertEqual(by_title["Movie 2"]["status_source"], "Transmission")
        self.assertEqual(by_title["Movie 3"]["lifecycle_status"], "submitted")
        self.assertEqual(by_title["Movie 3"]["status_label"], "已提交")
        self.assertEqual(by_title["Movie 3"]["status_source"], "MoviePilot")
        self.assertEqual(by_title["Movie 4"]["lifecycle_status"], "failed")
        self.assertEqual(by_title["Movie 4"]["status_label"], "失败")
        self.assertNotIn("secret", json.dumps(by_title["Movie 4"], ensure_ascii=False))
        self.assertEqual(by_title["Movie 5"]["lifecycle_status"], "pending_library")
        self.assertEqual(by_title["Movie 5"]["status_label"], "待入库")

    async def test_overview_random_posters_are_stable_and_not_rank_ordered(self) -> None:
        ordered = await main.overview()
        self.assertEqual([item["rank_no"] for item in ordered["recent_items"]], [1, 2, 3, 4, 5, 6])
        settings.dashboard_random_posters = True
        first = await main.overview()
        second = await main.overview()
        first_ranks = [item["rank_no"] for item in first["recent_items"]]
        self.assertEqual(first_ranks, [item["rank_no"] for item in second["recent_items"]])
        self.assertEqual(len(first_ranks), 6)
        # 随机海报必须来自已入库影片集合，不依赖 fixture 的 rank 奇偶约定。
        with connect() as conn:
            in_library_ranks = {
                to_int(row["rank_no"]) for row in conn.execute(
                    "SELECT rank_no FROM playlist_items WHERE playlist_id=? AND library_state='in_library'",
                    (self.playlist_id,),
                )
            }
        self.assertTrue(set(first_ranks).issubset(in_library_ranks))
        self.assertNotEqual(first_ranks, [1, 2, 3, 4, 5, 6])

    async def test_emby_poster_is_proxied_and_validated(self) -> None:
        with connect() as conn:
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE playlist_items SET emby_item_id='abc123',emby_image_tag='tag1' WHERE id=?", (item_id,),
            )
        original = main.EmbyClient.poster

        async def fake_poster(_client: object, _item_id: str) -> tuple[bytes, str]:
            return b"\x89PNG\r\n\x1a\nposter", "image/png"

        main.EmbyClient.poster = fake_poster
        try:
            response = await main.playlist_item_poster(to_int(item_id), "tag1")
        finally:
            main.EmbyClient.poster = original
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.body, b"\x89PNG\r\n\x1a\nposter")

    async def test_playlist_items_are_server_paginated_and_filtered(self) -> None:
        result = await main.playlist_items(self.playlist_id, page=2, page_size=100)
        self.assertEqual(result["total"], 250)
        self.assertEqual(result["page"], 2)
        self.assertEqual(len(result["items"]), 100)
        self.assertEqual(result["items"][0]["rank_no"], 101)
        filtered = await main.playlist_items(self.playlist_id, page=1, page_size=200, query="Movie 25")
        self.assertGreater(filtered["total"], 0)
        self.assertTrue(all("Movie 25" in item["original_title"] for item in filtered["items"]))
        capped = await main.playlist_items(self.playlist_id, page=1, page_size=10_000)
        self.assertEqual(capped["page_size"], 200)

    async def test_expired_cart_context_is_visible_and_recorded_safely(self) -> None:
        with connect() as conn:
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,size,group_tier,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("expired", task_id, item_id, 0, "Example 1080p", "Test", 1024, 9, 1, "{}", main.utc_now()),
            )
            conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", ("expired", main.utc_now()))
        items = await main.cart()
        self.assertFalse(items[0]["context_available"])
        with self.assertRaises(HTTPException) as raised:
            await main.download_cart()
        self.assertEqual(raised.exception.status_code, 409)
        with connect() as conn:
            history = conn.execute("SELECT success,message FROM download_history WHERE candidate_id='expired'").fetchall()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["success"], 0)
        self.assertNotIn("http", history[0]["message"].lower())

    async def test_source_refresh_reuses_item_ids_and_snapshots_removed_history(self) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE playlists SET source_url='https://letterboxd.com/test/list/sample/' WHERE id=?",
                (self.playlist_id,),
            )
            rows = conn.execute(
                "SELECT * FROM playlist_items WHERE playlist_id=? AND rank_no IN (1,2)", (self.playlist_id,),
            ).fetchall()
            retained_id, removed_id = to_int(rows[0]["id"]), to_int(rows[1]["id"])
            conn.execute(
                "INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at) VALUES(?,?,?,?,?)",
                (removed_id, "Movie 2", "Movie 2 2002 1080p", 1, main.utc_now()),
            )

        async def fetch_source(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "source_name": "刷新来源",
                "items": [{"rank_no": 1, "imdb_id": "tt0000001", "original_title": "Movie 1", "year": 2001}],
            }

        with patch("app.api.playlists.PlaylistSourceFetcher.fetch", new=fetch_source):
            result = await main.refresh_playlist_source(self.playlist_id)
        self.assertEqual(result["count"], 1)
        with connect() as conn:
            retained = conn.execute("SELECT id FROM playlist_items WHERE playlist_id=?", (self.playlist_id,)).fetchone()
            snapshot = conn.execute(
                "SELECT playlist_item_snapshot_json FROM download_history WHERE playlist_item_id=?",
                (removed_id,),
            ).fetchone()[0]
        self.assertEqual(to_int(retained["id"]), retained_id)
        self.assertIn("Movie 2", snapshot)
        self.assertNotEqual(retained_id, removed_id)
        with patch(
            "app.services.history.TransmissionClient.current_downloads",
            new=AsyncMock(return_value=[{"name": "Movie 2 2002 1080p", "status": 4, "percentDone": 0}]),
        ):
            history = await projected_download_history()
        removed_history = next(item for item in history if item["title"] == "Movie 2")
        self.assertEqual(removed_history["lifecycle_status"], "downloading")

    async def test_delete_playlist_preserves_history_snapshot(self) -> None:
        with connect() as conn:
            item = conn.execute(
                "SELECT * FROM playlist_items WHERE playlist_id=? AND rank_no=3", (self.playlist_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at) VALUES(?,?,?,?,?)",
                (item["id"], "Movie 3", "Movie 3 2003 1080p", 1, main.utc_now()),
            )
            item_id = to_int(item["id"])
        await playlist_routes.delete_playlist(self.playlist_id)
        with connect() as conn:
            snapshot = conn.execute(
                "SELECT playlist_item_snapshot_json FROM download_history WHERE playlist_item_id=?",
                (item_id,),
            ).fetchone()[0]
        self.assertIn("Movie 3", snapshot)

    async def test_concurrent_cart_submission_calls_moviepilot_once(self) -> None:
        with connect() as conn:
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO candidates(
                     id,task_id,playlist_item_id,candidate_index,title,site_name,ranking,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("concurrent", task_id, item_id, 0, "Movie.1.1080p", "Test", 1, "{}", main.utc_now()),
            )
            conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", ("concurrent", main.utc_now()))
        main.raw_candidates["concurrent"] = {
            "media": {"id": 1, "title": "Movie 1", "type": "电影"},
            "torrent": {"title": "Movie.1.1080p"},
        }
        entered = asyncio.Event()
        calls = 0

        async def delayed_download(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.sleep(0.03)
            return {"success": True, "hash": "one"}

        with patch.object(main.MoviePilotClient, "download", new=delayed_download):
            first = asyncio.create_task(main.download_cart())
            await entered.wait()
            with self.assertRaises(HTTPException) as raised:
                await main.download_cart()
            result = await first
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(calls, 1)

    async def test_source_refresh_rechecks_tasks_before_replacing_items(self) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE playlists SET source_url='https://letterboxd.com/test/list/sample/' WHERE id=?",
                (self.playlist_id,),
            )

        async def fetch_and_start_task(*_args: object, **_kwargs: object) -> dict[str, object]:
            with connect() as conn:
                conn.execute(
                    """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (self.playlist_id, 1, 1, "queued", 1, main.utc_now(), main.utc_now()),
                )
            return {
                "source_name": "竞态来源",
                "items": [{"rank_no": 1, "imdb_id": "tt9999999", "original_title": "Replacement", "year": 2025}],
            }

        with patch("app.api.playlists.PlaylistSourceFetcher.fetch", new=fetch_and_start_task):
            with self.assertRaises(HTTPException) as raised:
                await main.refresh_playlist_source(self.playlist_id)
        self.assertEqual(raised.exception.status_code, 409)
        with connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (self.playlist_id,),
            ).fetchone()[0]
            first_title = conn.execute(
                "SELECT original_title FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1",
                (self.playlist_id,),
            ).fetchone()[0]
        self.assertEqual(count, 250)
        self.assertEqual(first_title, "Movie 1")

    async def test_cleanup_preserves_download_history(self) -> None:
        with connect() as conn:
            conn.execute(
                """INSERT INTO download_history(title,torrent_name,success,message,created_at)
                   VALUES(?,?,?,?,datetime('now','-365 days'))""",
                ("Old Movie", "Old Torrent", 1, None),
            )
        cleanup_old_data()
        with connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM download_history WHERE title='Old Movie'",
            ).fetchone()
        self.assertIsNotNone(exists)

    async def test_cleanup_archives_failed_search_without_deleting_candidates(self) -> None:
        with connect() as conn:
            item_id = to_int(conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1",
                (self.playlist_id,),
            ).fetchone()[0])
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(
                       playlist_id,range_start,range_end,status,total,created_at,updated_at
                   ) VALUES(?,?,?,?,?,datetime('now','-2 days'),datetime('now','-2 days'))""",
                (self.playlist_id, 1, 1, "failed", 1),
            ).lastrowid)
            conn.execute(
                """INSERT INTO candidates(
                       id,task_id,playlist_item_id,candidate_index,title,group_tier,ranking,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,datetime('now','-2 days'))""",
                ("archived-candidate", task_id, item_id, 0, "保留候选", 9, 1, "{}"),
            )
        cleanup_old_data()
        with connect() as conn:
            task = conn.execute("SELECT status FROM search_tasks WHERE id=?", (task_id,)).fetchone()
            candidate = conn.execute("SELECT 1 FROM candidates WHERE id='archived-candidate'").fetchone()
        visible = await search_routes.search_tasks(limit=50)
        self.assertEqual(task["status"], "archived")
        self.assertIsNotNone(candidate)
        self.assertNotIn(task_id, [item["id"] for item in visible])

    async def test_history_clear_only_deletes_selected_lifecycle_group(self) -> None:
        with connect() as conn:
            conn.executemany(
                """INSERT INTO download_history(title,torrent_name,success,message,created_at)
                   VALUES(?,?,?,?,?)""",
                [
                    ("失败影片", "失败资源", 0, "提交失败", main.utc_now()),
                    ("成功影片", "成功资源", 1, None, main.utc_now()),
                ],
            )
        with patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])):
            deleted = await clear_download_history("failed")
        with connect() as conn:
            rows = conn.execute("SELECT title,success FROM download_history ORDER BY id").fetchall()
        self.assertEqual(deleted, 1)
        self.assertEqual([(row["title"], row["success"]) for row in rows], [("成功影片", 1)])

    async def test_initialize_scrubs_legacy_history(self) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO download_history(title,torrent_name,success,message,created_at) VALUES(?,?,?,?,?)",
                ("电影", "资源", 0, "https://tracker.test/a?passkey=legacy-secret", main.utc_now()),
            )
        initialize()
        with connect() as conn:
            message = conn.execute("SELECT message FROM download_history ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertNotIn("legacy-secret", message)
        self.assertIn("passkey=***", message)

    async def test_playlist_reorder_rejects_duplicate_ids(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await main.reorder_playlists(main.PlaylistOrderPayload(ids=[self.playlist_id, self.playlist_id]))
        self.assertEqual(raised.exception.status_code, 422)

    async def test_candidates_tolerate_corrupted_legacy_metadata(self) -> None:
        with connect() as conn:
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,group_tier,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("damaged", task_id, item_id, 0, "Example 1080p", 9, 1, "not-json", main.utc_now()),
            )
        result = await main.candidates(to_int(task_id))
        self.assertEqual(result[0]["metadata"], {})

    async def test_site_icon_blocks_cross_host_private_networks(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "内网"):
            await main.validate_remote_icon_url("http://127.0.0.1/icon.png", "https://example.com")
        await main.validate_remote_icon_url("http://127.0.0.1/icon.png", "http://127.0.0.1")

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

    async def test_searchable_queue_excludes_transmission_downloads_and_limits_front(self) -> None:
        with patch.object(
            main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[
                {"name": "Movie 2 2002 1080p", "status": 4, "percentDone": 0.25},
            ]),
        ):
            queue = await main.searchable_playlist_items(self.playlist_id, limit=50)
        self.assertEqual(queue["total_count"], 250)
        self.assertEqual(queue["in_library_count"], 125)
        self.assertEqual(queue["downloading_count"], 1)
        self.assertEqual(queue["pending_count"], 124)
        self.assertEqual(len(queue["items"]), 50)
        ranks = [item["rank_no"] for item in queue["items"]]
        self.assertNotIn(2, ranks)
        self.assertEqual(ranks, sorted(ranks))
        # 队列不得包含已入库影片，且下载中的影片被排除。
        with connect() as conn:
            in_library_ranks = {
                to_int(row["rank_no"]) for row in conn.execute(
                    "SELECT rank_no FROM playlist_items WHERE playlist_id=? AND library_state='in_library'",
                    (self.playlist_id,),
                )
            }
        self.assertTrue(all(rank not in in_library_ranks for rank in ranks))

    async def test_pending_search_task_persists_selected_item_ids(self) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,created_at) VALUES(?,?,?,?)",
                ("测试站点", "rss", "https://example.com/feed", main.utc_now()),
            )
        with patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])), \
             patch("app.api.search.run_search", new=AsyncMock()):
            result = await main.create_task(main.TaskPayload(playlist_id=self.playlist_id, scope="pending", count=50))
            await main.running_tasks[result["id"]]
            main.running_tasks.pop(result["id"], None)
        with connect() as conn:
            task = conn.execute("SELECT * FROM search_tasks WHERE id=?", (result["id"],)).fetchone()
        selected = json.loads(task["item_ids_json"])
        self.assertEqual(result["total"], 50)
        self.assertEqual(len(selected), 50)
        self.assertEqual(task["trigger"], "pending")
        self.assertTrue(json.loads(task["site_ids_json"]))

    async def test_persisted_search_capacity_blocks_a_fourth_task(self) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,created_at) VALUES(?,?,?,?)",
                ("容量站点", "rss", "https://example.com/feed", main.utc_now()),
            )
            now = main.utc_now()
            conn.executemany(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                [(self.playlist_id, 1, 1, "queued", 1, now, now) for _ in range(main.MAX_RUNNING_SEARCH_TASKS)],
            )
        with self.assertRaises(HTTPException) as raised:
            await main.create_task(main.TaskPayload(playlist_id=self.playlist_id, scope="range", range_start=1, range_end=1))
        self.assertEqual(raised.exception.status_code, 429)

    async def test_cleanup_archives_stale_failed_tasks_across_timestamp_formats(self) -> None:
        """cleanup_old_data 的日期比较必须兼容 ISO 'T' 与空格两种存储格式。"""
        with connect() as conn:
            now = main.utc_now()
            conn.executemany(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?) """,
                [
                    (self.playlist_id, 1, 1, "failed", 1, now, "2020-01-01T00:00:00+00:00"),
                    (self.playlist_id, 2, 2, "failed", 1, now, "2020-01-01 00:00:00"),
                    (self.playlist_id, 3, 3, "failed", 1, now, now),
                ],
            )
        main.cleanup_old_data()
        with connect() as conn:
            rows = conn.execute(
                "SELECT status FROM search_tasks WHERE playlist_id=? ORDER BY range_start", (self.playlist_id,),
            ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["archived", "archived", "failed"])

    async def test_cart_rejects_duplicate_release_and_download_skips_submitted(self) -> None:
        """同影片同站点同发布只能入车一次；已成功提交过的发布再次提交会被跳过。"""
        with connect() as conn:
            task_id = conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid
            item_id = to_int(conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0])
            now = main.utc_now()
            for candidate_id in ("dup-a", "dup-b"):
                conn.execute(
                    """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,size,eligibility,resource_key,ranking,metadata_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (candidate_id, task_id, item_id, 0, "Movie.2020.1080p.x265-FRDS", "Alpha", 8 * 1024**3,
                     "eligible", "movie20201080px265frds:128", 1, "{}", now),
                )
        main.raw_candidates["dup-a"] = {"media": {"id": 1}, "torrent": {"title": "t"}}
        main.raw_candidates["dup-b"] = {"media": {"id": 1}, "torrent": {"title": "t"}}
        await main.toggle_cart("dup-a")
        with self.assertRaises(HTTPException) as raised:
            await main.toggle_cart("dup-b")
        self.assertEqual(raised.exception.status_code, 422)
        # 已有成功提交记录时，再次提交同一发布应跳过而非重复下载。
        with connect() as conn:
            conn.execute(
                """INSERT INTO download_history(candidate_id,playlist_item_id,title,torrent_name,site_name,success,created_at)
                   VALUES(?,?,?,?,?,1,?)""",
                ("dup-a", item_id, "Movie 1", "Movie.2020.1080p.x265-FRDS", "Alpha", main.utc_now()),
            )
        with patch.object(main.MoviePilotClient, "download", new=AsyncMock()) as download_mock, \
             patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])):
            result = await main.download_cart()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["reason"], "该发布已提交过")
        download_mock.assert_not_awaited()

    async def test_resource_key_history_blocks_duplicate_after_candidate_cleanup(self) -> None:
        resource_key = "movie2020release:128"
        with connect() as conn:
            task_id = to_int(conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid)
            item_id = to_int(conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0])
            conn.execute(
                """INSERT INTO candidates(
                     id,task_id,playlist_item_id,candidate_index,title,site_name,size,eligibility,resource_key,ranking,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("old-release", task_id, item_id, 0, "Movie.2020.1080p", "Alpha", 8 * 1024**3, "eligible", resource_key, 1, "{}", main.utc_now()),
            )
            conn.execute(
                """INSERT INTO download_history(
                     candidate_id,playlist_item_id,resource_key,title,torrent_name,site_name,success,created_at
                   ) VALUES(?,?,?,?,?,?,1,?)""",
                ("old-release", item_id, resource_key, "Movie 1", "Movie.2020.1080p", "Alpha", main.utc_now()),
            )
            # 模拟 30 天候选清理：历史快照必须独立保留去重 key。
            conn.execute("DELETE FROM candidates WHERE id='old-release'")
            conn.execute(
                """INSERT INTO candidates(
                     id,task_id,playlist_item_id,candidate_index,title,site_name,size,eligibility,resource_key,ranking,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("new-release", task_id, item_id, 0, "Movie.2020.1080p", "Alpha", 8 * 1024**3, "eligible", resource_key, 1, "{}", main.utc_now()),
            )
            conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", ("new-release", main.utc_now()))
        main.raw_candidates["new-release"] = {"media": {"id": 1}, "torrent": {"title": "Movie.2020.1080p"}}
        with patch.object(main.MoviePilotClient, "download", new=AsyncMock()) as download_mock, \
             patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])):
            result = await main.download_cart()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "该发布已提交过")
        download_mock.assert_not_awaited()

    async def test_download_cart_skips_release_already_downloading(self) -> None:
        """Transmission 已有同名活动任务时，提交自动跳过。"""
        with connect() as conn:
            task_id = conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid
            item_id = to_int(conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0])
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,eligibility,resource_key,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("downloading-candidate", task_id, item_id, 0, "Movie.2020.1080p.x265-FRDS", "Alpha",
                 "eligible", "movie20201080px265frds:128", 1, "{}", main.utc_now()),
            )
            conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", ("downloading-candidate", main.utc_now()))
        main.raw_candidates["downloading-candidate"] = {"media": {"id": 1}, "torrent": {"title": "Movie.2020.1080p.x265-FRDS"}}
        with patch.object(main.MoviePilotClient, "download", new=AsyncMock()) as download_mock, \
             patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[
                 {"name": "Movie.2020.1080p.x265-FRDS", "status": 4, "percentDone": 0.3},
             ])):
            result = await main.download_cart()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "Transmission 正在下载")
        download_mock.assert_not_awaited()

    async def test_candidate_identity_rejects_sequel_and_plural_variants(self) -> None:
        item = {
            "tmdb_title": "教父", "tmdb_original_title": "The Godfather",
            "original_title": "The Godfather", "chinese_title": "教父", "year": 1972, "tmdb_year": 1972,
        }
        media = {"year": "1972"}
        accepted, reason = main.candidate_identity(item, media, "The.Godfather.Part.II.1080p")
        self.assertFalse(accepted)
        self.assertIn("续集或分卷", reason or "")
        accepted, reason = main.candidate_identity(item, media, "The.Godfathers.1972.1080p")
        self.assertFalse(accepted)
        self.assertIn("片名不匹配", reason or "")
        # 正式片名本身含 Part II 时不应被自己的种子标题拦截。
        item_sequel = {**item, "tmdb_original_title": "The Godfather Part II", "original_title": "The Godfather Part II", "tmdb_title": "教父2", "chinese_title": "教父2", "year": 1974, "tmdb_year": 1974}
        accepted, _ = main.candidate_identity(item_sequel, {"year": "1974"}, "The.Godfather.Part.II.1974.1080p")
        self.assertTrue(accepted)
        # 年份匹配的正片仍然通过。
        accepted, _ = main.candidate_identity(item, media, "The.Godfather.1972.1080p.BluRay")
        self.assertTrue(accepted)

    async def test_avc_bdrip_not_rejected_as_raw_disc(self) -> None:
        """AVC 编码的 BDrip 不再被误判为完整原盘。"""
        config = config_values()
        analyzed = main.analyze_candidate("Movie.2024.1080p.BluRay.AVC.LPCM-BDRIP", 0, config, {"seeders": 5, "volume_factor": 1})
        self.assertNotIn("原盘", analyzed.get("exclusion_reason") or "")
        still_rejected = main.analyze_candidate("Movie.2024.1080p.BluRay.AVC.DTS-HD.MA", 0, config, {"seeders": 5, "volume_factor": 1})
        self.assertIn("原盘", still_rejected.get("exclusion_reason") or "")

    async def test_nested_quantifier_release_group_rule_rejected(self) -> None:
        with self.assertRaises(main.HTTPException) as raised:
            await main.put_config(main.ConfigPayload(candidate_policy={
                **DEFAULT_POLICY, "custom_release_groups": ["(?:A+)+B"],
            }))
        self.assertEqual(raised.exception.status_code, 422)
        # 普通组后量词（无嵌套）仍然合法。
        catalog = release_group_catalog({**DEFAULT_POLICY, "custom_release_groups": ["(?:AB|CD)+"]})
        self.assertEqual(catalog["custom_count"], 1)

    async def test_xlsx_too_many_entries_rejected(self) -> None:
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for index in range(MAX_XLSX_ENTRIES + 1):
                zf.writestr(f"entry-{index}.xml", "<x/>")
        encoded = base64.b64encode(archive.getvalue()).decode()
        with self.assertRaises(HTTPException) as raised:
            await main.resolve_import(main.ImportPayload(xlsx_base64=encoded))
        self.assertEqual(raised.exception.status_code, 413)

    async def test_searchable_queue_keeps_history_without_candidate(self) -> None:
        """候选被删除的历史提交仍能排除下载中的影片。"""
        with connect() as conn:
            item = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=2", (self.playlist_id,),
            ).fetchone()
            conn.execute(
                """INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at)
                   VALUES(?,?,?,1,?)""",
                (item["id"], "Movie 2", "Movie 2 2002 1080p", main.utc_now()),
            )
        with patch.object(main.TransmissionClient, "current_downloads", new=AsyncMock(return_value=[
            {"name": "Movie 2 2002 1080p", "status": 4, "percentDone": 0.1},
        ])):
            queue = await main.searchable_playlist_items(self.playlist_id, limit=10)
        self.assertNotIn(2, [item["rank_no"] for item in queue["items"]])

    async def test_background_task_capacity_blocks_third_recognition(self) -> None:
        tasks: list[asyncio.Task[None]] = []
        try:
            for _index in range(MAX_RUNNING_RECOGNITION_TASKS):
                task = asyncio.create_task(asyncio.sleep(30))
                tasks.append(task)
                running_recognition_tasks[9000 + _index] = task
            with connect() as conn:
                other_id = conn.execute(
                    "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                    ("第二片单", 2, main.utc_now()),
                ).lastrowid
            with self.assertRaises(HTTPException) as raised:
                await playlist_routes.recognize_playlist(to_int(other_id))
            self.assertEqual(raised.exception.status_code, 429)
        finally:
            for task in tasks:
                task.cancel()
            for key in list(running_recognition_tasks):
                if key >= 9000:
                    running_recognition_tasks.pop(key, None)

    async def test_automation_queues_when_capacity_full_and_consumes_later(self) -> None:
        from app.services.automation import consume_queued_automation_runs, start_playlist_automation
        tasks: list[asyncio.Task[None]] = []
        try:
            for index in range(MAX_RUNNING_AUTOMATION_TASKS):
                task = asyncio.create_task(asyncio.sleep(30))
                tasks.append(task)
                running_automation_tasks[8000 + index] = task
            result = await start_playlist_automation(self.playlist_id)
            self.assertEqual(result["status"], "queued")
            self.assertNotIn(result["id"], running_automation_tasks)
            # 容量释放后由调度器消费并启动
            for task in tasks:
                task.cancel()
            tasks.clear()
            for key in list(running_automation_tasks):
                if key >= 8000:
                    running_automation_tasks.pop(key, None)
            with patch("app.services.automation.run_playlist_automation", new=AsyncMock()) as runner:
                started = await consume_queued_automation_runs()
            self.assertEqual(started, 1)
            await asyncio.sleep(0)  # 让被创建的后台任务实际执行
            runner.assert_awaited_once()
        finally:
            for task in tasks:
                task.cancel()
            for key in list(running_automation_tasks):
                if key >= 8000:
                    running_automation_tasks.pop(key, None)

    async def test_validated_base_url_rejects_private_only_domain_with_token(self) -> None:
        """启用访问令牌时，出站地址域名必须全部解析到公网；白名单与 IP 字面量规则生效。"""
        previous_token = os.environ.get("AUTOLIST_ACCESS_TOKEN")
        os.environ["AUTOLIST_ACCESS_TOKEN"] = "test-token"
        previous_hosts = os.environ.get("AUTOLIST_ALLOW_PRIVATE_HOSTS")
        try:
            with patch("app.util.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.5", 0))]):
                with self.assertRaises(HTTPException) as raised:
                    system_routes.validated_base_url("https://private.example", "站点地址", True)
                self.assertEqual(raised.exception.status_code, 422)
            with patch("app.util.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
                result = system_routes.validated_base_url("https://public.example", "站点地址", True)
            self.assertEqual(result, "https://public.example")
            with patch("app.util.socket.getaddrinfo", side_effect=socket.gaierror("nxdomain")):
                with self.assertRaises(HTTPException):
                    system_routes.validated_base_url("https://nx.example", "站点地址", True)
            os.environ["AUTOLIST_ALLOW_PRIVATE_HOSTS"] = "prowlarr.lan"
            with patch("app.util.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.9", 0))]):
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

    async def test_zero_seeder_candidate_excluded(self) -> None:
        """0 人做种的资源无法下载，直接排除；有做种者的同标题资源不受影响。"""
        config = config_values()
        excluded = main.analyze_candidate(
            "Movie.2024.1080p.x265-FRDS", 0, config, {"seeders": 0, "volume_factor": 1},
        )
        self.assertFalse(excluded["eligible"])
        self.assertIn("0 人做种", excluded.get("exclusion_reason") or "")
        eligible = main.analyze_candidate(
            "Movie.2024.1080p.x265-FRDS", 0, config, {"seeders": 3, "volume_factor": 1},
        )
        self.assertTrue(eligible["eligible"])

    async def test_spoofed_release_group_codec_rejected(self) -> None:
        """Fury（x265 组）与 SPM（x264 组）编码不符时判定为冒用组名；@站点名 不再误识别为 HDS 组。"""
        config = config_values()
        spoofed = main.analyze_candidate(
            "Movie.2024.1080p.BluRay.x264-Fury@HDSky", 0, config, {"seeders": 5, "volume_factor": 1},
        )
        self.assertFalse(spoofed["eligible"])
        self.assertIn("FURY 组为 x265", spoofed.get("exclusion_reason") or "")
        spoofed = main.analyze_candidate(
            "Movie.2024.2160p.x265-SPM@HDSky", 0, config, {"seeders": 5, "volume_factor": 1},
        )
        self.assertFalse(spoofed["eligible"])
        self.assertIn("SPM 组为 x264", spoofed.get("exclusion_reason") or "")
        # 编码与知名组惯例一致时不再误报冒用；组名识别为 FURY（而非 HDSky 的 HDS）。
        genuine = main.analyze_candidate(
            "Movie.2024.1080p.x265-Fury@HDSky", 0, config, {"seeders": 5, "volume_factor": 1},
        )
        self.assertEqual(genuine["group"], "FURY")
        self.assertNotIn("疑似冒用", genuine.get("exclusion_reason") or "")

    async def test_aggregated_candidate_prefers_most_seeders(self) -> None:
        """同资源跨站点折叠时，主推荐必须是做种人数最多的发布，而不是站点优先级最高的。"""
        with connect() as conn:
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,priority,created_at) VALUES(?,?,?,?,?)",
                ("聚合测试站A", "nexusphp", "https://site-a.example", 1, main.utc_now()),
            )
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,priority,created_at) VALUES(?,?,?,?,?)",
                ("聚合测试站B", "nexusphp", "https://site-b.example", 5, main.utc_now()),
            )
            for index, (site, seeders) in enumerate([("聚合测试站A", 2), ("聚合测试站B", 80)]):
                conn.execute(
                    """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,size,seeders,
                               resolution,codec,group_name,group_tier,score,score_breakdown,ranking,recommendation,
                               recommendation_reason,resource_key,library_state,is_manual_only,eligibility,exclusion_reason,
                               profile_id,metadata_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"same-{index}", task_id, item_id, index, "Movie.2024.1080p.x265-FRDS", site, 1000, seeders,
                     "1080p", "x265", "FRDS", 1, 90, "[]", index, "preferred", "", "movie20241080px265frds:0",
                     "unknown", 0, "eligible", None, "primary_x265", "{}", main.utc_now()),
                )
        result = await main.candidates(to_int(task_id))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["site_name"], "聚合测试站B")
        self.assertEqual(result[0]["seeders"], 80)
        self.assertEqual(result[0]["site_count"], 2)
        self.assertEqual(len(result[0]["site_options"]), 2)
        options_by_site = {option["site_name"]: option for option in result[0]["site_options"]}
        self.assertEqual(options_by_site["聚合测试站A"]["seeders"], 2)
        self.assertEqual(options_by_site["聚合测试站B"]["seeders"], 80)


    async def test_nexusphp_banner_stats_parses_home_welcome_block(self) -> None:
        """NexusPHP 账户统计优先从首页欢迎横幅解析，不依赖用户详情页。"""
        client = main.NexusPHPClient()
        banner = client._banner_stats(
            "<b>欢迎回来</b> 上传量：16.605 TB 下载量：1.062 TB "
            "分享率：15.631 魔力值 [ 2,345.67 ] 当前活动： 3 1"
        )
        self.assertIsNotNone(banner)
        assert banner is not None
        self.assertEqual(banner["uploaded"], 18257390579220)
        self.assertEqual(banner["downloaded"], 1167681348698)
        self.assertEqual(banner["ratio"], 15.631)
        self.assertEqual(banner["bonus"], 2345.67)
        self.assertEqual(banner["seeding"], 3)
        # 繁体标签与英文标签同样兼容；无横幅时返回 None。
        alt = client._banner_stats("上傳量: 1.5 TiB 下載量: 200 GiB 分享率: 7.68 做種數: 12")
        self.assertIsNotNone(alt)
        assert alt is not None
        self.assertEqual(alt["ratio"], 7.68)
        self.assertEqual(alt["seeding"], 12)
        self.assertIsNone(client._banner_stats("<html><body>spinner page</body></html>"))

    async def test_tnode_account_stats_from_user_api(self) -> None:
        """TNode SPA 站点（朱雀等）从 /api/user/getInfo 计算上传/下载/分享率。"""
        client = main.NexusPHPClient()
        stats = client._tnode_stats({"upload": 377842816983, "download": 92373125738, "bonus": 774586.57, "seeding": 0})
        self.assertIsNotNone(stats)
        assert stats is not None
        self.assertEqual(stats["uploaded"], 377842816983)
        self.assertEqual(stats["downloaded"], 92373125738)
        self.assertAlmostEqual(float(stats["ratio"] or 0), 377842816983 / 92373125738, places=6)
        self.assertEqual(stats["seeding"], 0)
        # 下载量为 0 时分享率置空（避免除零），上传下载均为 0 时视为无数据。
        zero = client._tnode_stats({"upload": 100, "download": 0})
        self.assertIsNone(zero["ratio"])
        self.assertIsNone(client._tnode_stats({}))


    async def test_log_events_api_reads_json_lines_newest_first(self) -> None:
        """日志接口从 data/logs/autolist.log 读取结构化事件，倒序返回并支持过滤。"""
        logs_dir = Path(self.temp.name) / "logs"
        logs_dir.mkdir(exist_ok=True)
        log_file = logs_dir / "autolist.log"
        log_file.write_text(
            "{\"ts\": \"2026-08-01T10:00:00+00:00\", \"level\": \"INFO\", \"event\": \"movie_search_summary\", \"rank\": 1, \"movie\": \"Dreams\", \"results\": 140, \"kept\": 1}\n"
            "{\"ts\": \"2026-08-01T10:01:00+00:00\", \"level\": \"WARNING\", \"event\": \"site_search_failed\", \"site\": \"春天\", \"error\": \"ReadTimeout\"}\n"
            "{\"ts\": \"2026-08-01T10:02:00+00:00\", \"level\": \"ERROR\", \"event\": \"search_task_finished\", \"task_id\": 32, \"status\": \"failed\"}\n",
            encoding="utf-8",
        )
        from app.api import logs as log_routes
        events = await log_routes.log_events()
        self.assertEqual([event["event"] for event in events], ["search_task_finished", "site_search_failed", "movie_search_summary"])
        warnings = await log_routes.log_events(level="WARNING")
        self.assertEqual([event["event"] for event in warnings], ["site_search_failed"])
        filtered = await log_routes.log_events(query="Dreams")
        self.assertEqual([event["event"] for event in filtered], ["movie_search_summary"])
        self.assertEqual(await log_routes.log_events(limit=1), [events[0]])
        # 清空日志后读取为空。
        await log_routes.clear_log_events()
        self.assertEqual(await log_routes.log_events(), [])

    async def test_configure_logging_writes_rotating_json_file(self) -> None:
        """configure_logging 在数据目录创建 JSON 行日志，事件字段进入文件。"""
        from app.logs import configure_logging, event_logger
        configure_logging()
        logger = event_logger()
        logger.info("movie_search_summary", extra={"task_id": 1, "rank": 3, "movie": "Macario", "results": 0, "kept": 0})
        log_file = Path(self.temp.name) / "logs" / "autolist.log"
        self.assertTrue(log_file.exists())
        content = log_file.read_text(encoding="utf-8")
        self.assertIn("movie_search_summary", content)
        self.assertIn("Macario", content)
        self.assertIn("results", content)
        event = json.loads(content.strip().splitlines()[-1])
        self.assertEqual(event["movie"], "Macario")
        self.assertEqual(event["results"], 0)


    async def test_nexusphp_publish_time_parses_absolute_and_relative(self) -> None:
        """NexusPHP 列表行发布时间：绝对日期（含 / 与中文格式）与相对时间（x月 x天/昨天/小时）均解析。"""
        client = main.NexusPHPClient()
        self.assertEqual(client._parse_publish_time(["Dune 2024", "2026-04-22"]), "2026-04-22")
        self.assertEqual(client._parse_publish_time(["", "2026/04/22"]), "2026-04-22")
        self.assertEqual(client._parse_publish_time(["", "2026年4月22日"]), "2026-04-22")
        self.assertIsNone(client._parse_publish_time(["", "无时间列"]))
        # 相对时间换算为今天的绝对日期（偏移量级正确即可）。
        relative = client._parse_publish_time(["", "2月 2天"])
        self.assertIsNotNone(relative)
        assert relative is not None
        import datetime as _dt
        expected = (_dt.date.today() - _dt.timedelta(days=62)).isoformat()
        self.assertEqual(relative, expected)
        self.assertEqual(client._parse_publish_time(["", "昨天"]), (_dt.date.today() - _dt.timedelta(days=1)).isoformat())
        self.assertEqual(client._parse_publish_time(["", "3 小时前"]), _dt.date.today().isoformat())



class FrontendContractTests(unittest.TestCase):
    """2-25：前端契约从字符串计数升级为引用一致性——app.js 的 ES 模块导入必须可解析。"""

    def test_frontend_module_contract_js_imports_resolve(self) -> None:
        from pathlib import Path
        import re

        app_js = Path("app/static/app.js").read_text(encoding="utf-8")
        import_line = next(line for line in app_js.splitlines() if line.startswith("import {"))
        imported = [name.strip() for name in import_line.split("{", 1)[1].split("}", 1)[0].split(",")]
        core_js = Path("app/static/js/core.js").read_text(encoding="utf-8")
        missing = [name for name in imported if f"export const {name}" not in core_js and f"export function {name}" not in core_js]
        self.assertEqual(missing, [])

        module_imports = re.findall(r'^import\s*\{(.*?)\}\s*from\s*"([^"]+)";', app_js, re.M | re.S)
        site_body = next((body for body, path in module_imports if path == "./js/site-map.js"), None)
        self.assertIsNotNone(site_body)
        site_imported = [name.strip() for name in site_body.split(",") if name.strip()]
        site_map_js = Path("app/static/js/site-map.js").read_text(encoding="utf-8")
        site_exports = set(re.findall(r"export (?:const|let|function) ([A-Za-z_$][\w$]*)", site_map_js))
        self.assertEqual([name for name in site_imported if name not in site_exports], [])


if __name__ == "__main__":
    unittest.main()
