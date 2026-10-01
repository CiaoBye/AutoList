"""应用外壳：静态资源缓存、界面约定、日志与未处理异常。"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.support import IsolatedAppTestCase, SeededPlaylistTestCase


class ShellResourceTests(unittest.TestCase):
    def test_shell_resources_revalidate_after_deploy(self) -> None:
        previous_data_dir = settings.data_dir
        with tempfile.TemporaryDirectory() as temp:
            settings.data_dir = temp
            try:
                with TestClient(app) as client:
                    index = client.get("/")
                    favicon = client.get("/favicon.ico")
            finally:
                settings.data_dir = previous_data_dir
        self.assertEqual(index.status_code, 200)
        self.assertEqual(favicon.status_code, 200)
        self.assertEqual(index.headers.get("cache-control"), "no-cache, must-revalidate")
        self.assertEqual(favicon.headers.get("cache-control"), "no-cache, must-revalidate")


class LoggingTests(SeededPlaylistTestCase):
    async def test_log_events_api_reads_json_lines_newest_first(self) -> None:
        """日志接口从 data/logs/autolist.log 读取结构化事件，倒序返回并支持过滤。"""
        logs_dir = Path(self.temp.name) / "logs"
        logs_dir.mkdir(exist_ok=True)
        log_file = logs_dir / "autolist.log"
        log_file.write_text(
            "{\"ts\": \"2026-08-01T10:00:00+00:00\", \"level\": \"INFO\", \"event\": \"movie_search_summary\", \"rank\": 1, \"movie\": \"Dreams\", \"results\": 140, \"kept\": 1}\n"
            "{\"ts\": \"2026-08-01T10:01:00+00:00\", \"level\": \"WARNING\", \"event\": \"site_search_failed\", \"site\": \"站点B\", \"error\": \"ReadTimeout\"}\n"
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

    async def test_log_events_page_backwards_and_accept_several_levels(self) -> None:
        logs_dir = Path(self.temp.name) / "logs"
        logs_dir.mkdir(exist_ok=True)
        lines = [
            {"ts": "2026-08-01T10:00:00+00:00", "level": "INFO", "event": "a"},
            {"ts": "2026-08-01T10:01:00+00:00", "level": "WARNING", "event": "b"},
            {"ts": "2026-08-01T10:02:00+00:00", "level": "ERROR", "event": "c"},
            {"ts": "2026-08-01T10:02:00+00:00", "level": "INFO", "event": "d"},
            {"ts": "2026-08-01T10:03:00.500000+00:00", "level": "INFO", "event": "e"},
        ]
        (logs_dir / "autolist.log").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        from app.api import logs as log_routes

        first = await log_routes.log_events(limit=2)
        # 本页最早一条（d）与 c 同一时刻，c 一并返回，下一页不会漏掉或重复。
        self.assertEqual([event["event"] for event in first], ["e", "d", "c"])
        rest = await log_routes.log_events(limit=2, before=first[-1]["ts"])
        self.assertEqual([event["event"] for event in rest], ["b", "a"])
        self.assertEqual(await log_routes.log_events(before="2026-08-01T10:00:00+00:00"), [])
        problems = await log_routes.log_events(level="warning,ERROR")
        self.assertEqual([event["event"] for event in problems], ["c", "b"])
        with self.assertRaises(HTTPException):
            await log_routes.log_events(before="not-a-time")

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


class StaticAssetCacheTests(IsolatedAppTestCase):
    async def test_static_modules_are_revalidated(self) -> None:
        from app.main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/assets/logo.svg")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("cache-control"), "no-cache")
            etag = response.headers.get("etag")
            self.assertTrue(etag)
            revalidated = await client.get("/assets/logo.svg", headers={"If-None-Match": etag})
            self.assertEqual(revalidated.status_code, 304)


class InterfaceContractTests(unittest.TestCase):
    """新界面（frontend/ → app/static/ui）的静态契约：旧界面已移除，主题只改 token 不改组件。"""

    root = Path(__file__).resolve().parents[1]

    def test_legacy_interface_is_removed(self) -> None:
        static = self.root / "app" / "static"
        for name in ("index.html", "app.js", "style.css", "theme.css", "js/core.js", "js/site-map.js", "js/theme-init.js"):
            self.assertFalse((static / name).exists(), name)
        # 新设置页仍引用的品牌与服务图标保留。
        for name in ("logo.svg", "favicon.svg", "autolist-icon.png"):
            self.assertTrue((static / name).exists(), name)

    def test_themes_only_switch_tokens(self) -> None:
        styles = self.root / "frontend" / "src"
        self.assertNotIn("[data-theme", (styles / "app.css").read_text(encoding="utf-8"))
        self.assertIn('[data-theme="cinema"]', (styles / "tokens.css").read_text(encoding="utf-8"))

    def test_format_size_reaches_terabytes(self) -> None:
        script = (
            "import { formatSize } from './frontend/src/format.ts';"
            "const G = 1024 ** 3;"
            "console.log(JSON.stringify([0, 5 * G, 48294 * G, 250 * 1024 * G, 3 * 1024 ** 2].map(formatSize)));"
        )
        output = subprocess.check_output(
            ["node", "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", script],
            cwd=self.root, text=True,
        )
        self.assertEqual(json.loads(output), ["—", "5.0 GB", "47.2 TB", "250 TB", "3 MB"])


class LogFormatterAndErrorHandlerTests(IsolatedAppTestCase):
    def test_log_formatter_sanitizes_traceback_and_event(self):
        import json
        import logging

        from app.logs import JsonLineFormatter

        formatter = JsonLineFormatter()
        record = logging.LogRecord(
            name="autolist",
            level=logging.ERROR,
            pathname=__file__,
            lineno=10,
            msg="Request failed for https://site.com/download.php?passkey=abcdef1234567890",
            args=(),
            exc_info=None,
        )
        try:
            raise ValueError("Secret token: Bearer super_secret_access_token_12345")
        except ValueError:
            import sys
            record.exc_info = sys.exc_info()

        formatted = formatter.format(record)
        payload = json.loads(formatted)
        self.assertNotIn("abcdef1234567890", payload["event"])
        self.assertIn("passkey=***", payload["event"])
        self.assertIn("error", payload)
        self.assertNotIn("super_secret_access_token_12345", payload["error"])
        self.assertIn("***", payload["error"])

    def test_unhandled_exception_handler(self):
        from starlette.testclient import TestClient

        from app.main import app

        @app.get("/test-unhandled-error", include_in_schema=False)
        async def _faulty_endpoint():
            raise RuntimeError("Database connection secret_key=xyz exploded")

        # 测试路由挂在全局应用上，结束后移除，不留在其他用例看到的接口里。
        route = app.router.routes[-1]
        self.addCleanup(app.router.routes.remove, route)

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test-unhandled-error")
        self.assertEqual(response.status_code, 500)
        data = response.json()
        self.assertEqual(data.get("detail"), "系统内部错误，请稍后重试")
        self.assertNotIn("secret_key", response.text)
        self.assertNotIn("exploded", response.text)
