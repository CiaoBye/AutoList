"""接口契约：前端类型所依据的接口声明与实际返回一致。

- ``frontend/openapi.json`` 必须与当前接口声明一致（改了返回格式要重新导出，前端类型随之更新）；
- 用覆盖全部影片状态的样本数据经 HTTP 请求各读取接口，返回值必须通过声明的格式校验。
"""

from __future__ import annotations

import json

import httpx

from app.database import connect
from app.main import app
from app.openapi import SNAPSHOT_PATH, openapi_document
from app.util import utc_now
from tests.support import FilmFixture, IsolatedAppTestCase


class OpenApiSnapshotTests(IsolatedAppTestCase):
    async def test_frontend_snapshot_matches_the_declared_api(self) -> None:
        self.assertEqual(
            SNAPSHOT_PATH.read_text(encoding="utf-8"), openapi_document(),
            "接口声明已变化：运行 .venv/bin/python scripts/export_openapi.py 更新 frontend/openapi.json",
        )

    async def test_frontend_reads_are_declared(self) -> None:
        document = json.loads(openapi_document())
        undeclared = []
        for path, operations in document["paths"].items():
            for method, operation in operations.items():
                schema = operation["responses"]["200"]["content"].get("application/json", {}).get("schema", {})
                # 图片、页面入口等不返回 JSON 的接口没有 schema；返回任意字典的接口只有 additionalProperties: true。
                if method == "get" and path.startswith("/api/") and schema.get("additionalProperties") is True:
                    undeclared.append(path)
        # 这些读取接口前端不使用或只用于诊断，暂不声明返回格式。
        self.assertEqual(sorted(undeclared), ["/api/health", "/api/sites/{site_id}/health-history"])


    async def test_declared_film_states_match_the_status_service(self) -> None:
        from typing import get_args

        from app import responses
        from app.services.films import FILM_ISSUE_LABELS, FILM_STATUS_LABELS

        self.assertEqual(set(get_args(responses.FilmStatus)), set(FILM_STATUS_LABELS))
        self.assertEqual(set(get_args(responses.FilmIssue)), set(FILM_ISSUE_LABELS))


class ResponseContractTests(FilmFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        now = utc_now()
        with connect() as conn:
            conn.execute(
                "INSERT INTO search_task_logs(task_id,level,stage,message,created_at) VALUES(?,?,?,?,?)",
                (self.task_id, "info", "search", "开始寻片", now),
            )
            conn.execute(
                "INSERT INTO notifications(level,title,message,created_at) VALUES(?,?,?,?)",
                ("warning", "同步提醒", "来源暂时无法访问", now),
            )

    async def _get(self, client: httpx.AsyncClient, path: str) -> object:
        response = await client.get(path)
        self.assertEqual(response.status_code, 200, f"{path}: {response.text[:300]}")
        return response.json()

    async def test_every_read_passes_its_declared_format(self) -> None:
        paths = [
            "/api/films", f"/api/films?playlist_id={self.playlist_id}&status=issue:submit_failed",
            *[f"/api/films/{item_id}" for item_id in self.items.values()],
            "/api/home", f"/api/home?playlist_id={self.playlist_id}", "/api/picks", "/api/timeline",
            "/api/selection", "/api/history",
            "/api/search-tasks", f"/api/search-tasks/{self.task_id}", f"/api/search-tasks/{self.task_id}/attempts",
            f"/api/search-tasks/{self.task_id}/logs",
            "/api/sites", "/api/playlists", "/api/settings", "/api/cookiecloud/status", "/api/config",
            "/api/config/release-groups", "/api/logs/events",
        ]
        transport = httpx.ASGITransport(app=app)
        with self._no_transmission():
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                results = {path: await self._get(client, path) for path in paths}
                preview = await client.post("/api/config/score-preview", json={"title": "Movie.2020.1080p.BluRay.x265-CHD", "seeders": 3})
        self.assertEqual(preview.status_code, 200, preview.text)
        detail = results[f"/api/films/{self.items['candidates']}"]
        self.assertEqual(detail["candidates"][0]["site_options"][0]["site_name"], "春天")
        self.assertEqual(results["/api/picks"]["counts"]["all"], 3)
        self.assertEqual(results[f"/api/search-tasks/{self.task_id}"]["attempt_summary"]["total"], 3)
        # 声明之外的字段不会返回：站点列表不带账户统计的原始列。
        self.assertNotIn("account_uploaded", results["/api/sites"][0])
