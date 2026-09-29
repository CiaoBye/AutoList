"""后台任务：四类任务的登记、容量、取消与重启恢复，新片处理状态机与调度器健康。"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import playlists as playlist_routes
from app.api.playlists import configure_playlist_sync
from app.api.search import create_task
from app.config import settings
from app.database import connect, initialize
from app.healthcheck import validate_scheduler_health
from app.main import app
from app.schemas import PlaylistSyncPayload, TaskPayload
from app.services.automation import run_playlist_automation
from app.state import mark_scheduler_heartbeat, mark_scheduler_started, mark_scheduler_success, scheduler_health
from app.tasks import AUTOMATION, KINDS, LIBRARY, RECOGNITION, SEARCH, active_tasks, recover_after_restart
from app.util import to_int, utc_now
from tests.support import IsolatedAppTestCase, SeededPlaylistTestCase


class TaskFrameworkTests(IsolatedAppTestCase):
    def _playlist(self) -> int:
        with connect() as conn:
            return to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)", ("任务片单", 1, utc_now()),
            ).lastrowid)

    async def test_finished_tasks_leave_the_registry(self) -> None:
        async def work() -> None:
            await asyncio.sleep(0)

        task = LIBRARY.start(41, work())
        self.assertTrue(LIBRARY.is_running(41))
        await task
        await asyncio.sleep(0)
        self.assertNotIn(41, LIBRARY.running)

    async def test_capacity_and_cancel_behave_the_same_for_every_kind(self) -> None:
        gate = asyncio.Event()

        async def blocked() -> None:
            await gate.wait()

        for kind in KINDS:
            for index in range(kind.limit):
                kind.start(100 + index, blocked())
            self.assertEqual(kind.capacity_error(), f"已有 {kind.limit} 个{kind.label}任务在运行，请稍后再试")
            self.assertTrue(await kind.cancel(100))
            self.assertIsNone(kind.capacity_error())
            self.assertFalse(await kind.cancel(999))
        # 寻片按数据库里的排队数计算容量（多进程共享）。
        self.assertIsNotNone(SEARCH.capacity_error(SEARCH.limit))

    async def test_restart_interrupts_unfinished_tasks_but_keeps_queued_automation(self) -> None:
        playlist_id = self._playlist()
        now = utc_now()
        with connect() as conn:
            conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (playlist_id, 1, 1, "running", 1, now, now),
            )
            conn.execute(
                "INSERT INTO library_scan_tasks(playlist_id,status,total,created_at,updated_at) VALUES(?,?,?,?,?)",
                (playlist_id, "queued", 1, now, now),
            )
            conn.execute(
                "INSERT INTO automation_runs(playlist_id,trigger,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (playlist_id, "manual", "queued", "queued", now, now),
            )
            self.assertEqual(sorted(task["kind"] for task in active_tasks(conn)), ["automation", "library", "search"])
            recover_after_restart(conn)
            statuses = {
                kind.key: [row[0] for row in conn.execute(f"SELECT status FROM {kind.table}")]  # nosec B608
                for kind in KINDS
            }
        self.assertEqual(statuses["search"], ["interrupted"])
        self.assertEqual(statuses["library"], ["interrupted"])
        self.assertEqual(statuses[AUTOMATION.key], ["queued"])


class TaskCapacityAndHealthcheckTests(unittest.TestCase):
    def test_search_task_capacity_is_enforced(self) -> None:
        previous = dict(SEARCH.running)
        SEARCH.running.clear()

        class Alive:
            def done(self) -> bool:
                return False

        try:
            for index in range(SEARCH.limit):
                SEARCH.running[index] = Alive()  # type: ignore[assignment]
            rejection = SEARCH.capacity_error()
            self.assertIsNotNone(rejection)
            self.assertIn("寻片任务", rejection or "")
        finally:
            SEARCH.running.clear()
            SEARCH.running.update(previous)

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


class TaskCapacityApiTests(SeededPlaylistTestCase):
    async def test_nonempty_automation_uses_canonical_titles_without_auto_select(self) -> None:
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
            now = utc_now()
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
            selected_count = conn.execute("SELECT COUNT(*) FROM selection_items").fetchone()[0]
        self.assertEqual(run["status"], "completed")
        self.assertIn("候选需人工确认", run["message"])
        self.assertEqual(selected_count, 0)
        with self.assertRaises(HTTPException) as raised:
            await configure_playlist_sync(
                self.playlist_id, PlaylistSyncPayload(enabled=True, interval_hours=24),
            )
        self.assertEqual(raised.exception.status_code, 422)

    async def test_persisted_search_capacity_blocks_a_fourth_task(self) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,created_at) VALUES(?,?,?,?)",
                ("容量站点", "rss", "https://example.com/feed", utc_now()),
            )
            now = utc_now()
            conn.executemany(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                [(self.playlist_id, 1, 1, "queued", 1, now, now) for _ in range(SEARCH.limit)],
            )
        with self.assertRaises(HTTPException) as raised:
            await create_task(TaskPayload(playlist_id=self.playlist_id, scope="range", range_start=1, range_end=1))
        self.assertEqual(raised.exception.status_code, 429)

    async def test_background_task_capacity_blocks_third_recognition(self) -> None:
        tasks: list[asyncio.Task[None]] = []
        try:
            for _index in range(RECOGNITION.limit):
                task = asyncio.create_task(asyncio.sleep(30))
                tasks.append(task)
                RECOGNITION.running[9000 + _index] = task
            with connect() as conn:
                other_id = conn.execute(
                    "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                    ("第二片单", 2, utc_now()),
                ).lastrowid
            with self.assertRaises(HTTPException) as raised:
                await playlist_routes.recognize_playlist(to_int(other_id))
            self.assertEqual(raised.exception.status_code, 429)
        finally:
            for task in tasks:
                task.cancel()
            for key in list(RECOGNITION.running):
                if key >= 9000:
                    RECOGNITION.running.pop(key, None)

    async def test_automation_queues_when_capacity_full_and_consumes_later(self) -> None:
        from app.services.automation import consume_queued_automation_runs, start_playlist_automation
        tasks: list[asyncio.Task[None]] = []
        try:
            for index in range(AUTOMATION.limit):
                task = asyncio.create_task(asyncio.sleep(30))
                tasks.append(task)
                AUTOMATION.running[8000 + index] = task
            result = await start_playlist_automation(self.playlist_id)
            self.assertEqual(result["status"], "queued")
            self.assertNotIn(result["id"], AUTOMATION.running)
            # 容量释放后由调度器消费并启动
            for task in tasks:
                task.cancel()
            tasks.clear()
            for key in list(AUTOMATION.running):
                if key >= 8000:
                    AUTOMATION.running.pop(key, None)
            with patch("app.services.automation.run_playlist_automation", new=AsyncMock()) as runner:
                started = await consume_queued_automation_runs()
            self.assertEqual(started, 1)
            await asyncio.sleep(0)  # 让被创建的后台任务实际执行
            runner.assert_awaited_once()
        finally:
            for task in tasks:
                task.cancel()
            for key in list(AUTOMATION.running):
                if key >= 8000:
                    AUTOMATION.running.pop(key, None)


class AutomationStateMachineTests(IsolatedAppTestCase):
    """2-1/2-2：自动化 run 在容量满与搜索失败时的状态机回归测试。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # 识别任务在未配置 TMDB 时会直接失败；这些用例模拟的是已配置 TMDB 的识别流程。
        settings.tmdb_api_key = "test-tmdb-key"

    def _playlist_and_items(self, count: int = 1) -> tuple[int, list[int]]:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("后台任务测试", 1, utc_now()),
            ).lastrowid)
            item_ids = []
            for rank in range(1, count + 1):
                item_ids.append(to_int(conn.execute(
                    """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title)
                       VALUES(?,?,?,?,?,?)""",
                    (playlist_id, rank, f"tt{rank:07d}", f"Movie {rank}", 2020 + rank, f"电影 {rank}"),
                ).lastrowid))
        return playlist_id, item_ids

    def _automation_run(self, playlist_id: int) -> int:
        with connect() as conn:
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at) VALUES(?,?,?,1,1,?)",
                ("TestSite", "nexusphp", "https://tracker.example", utc_now()),
            )
            return to_int(conn.execute(
                """INSERT INTO automation_runs(playlist_id,trigger,status,stage,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (playlist_id, "manual", "queued", "queued", utc_now(), utc_now()),
            ).lastrowid)

    def _queue_item(self, playlist_id: int, item_id: int, rank: int = 1) -> dict:
        return {
            "id": item_id, "rank_no": rank, "imdb_id": "tt0000001", "tmdb_id": 42,
            "tmdb_title": "Movie 1", "tmdb_original_title": "Movie 1", "tmdb_year": "2021",
            "tmdb_imdb_id": "tt0000001", "original_title": "Movie 1", "year": 2021,
            "chinese_title": None, "library_state": "not_found",
        }

    async def test_capacity_full_keeps_run_queued(self) -> None:
        from fastapi import HTTPException

        from app.services import automation
        playlist_id, item_ids = self._playlist_and_items(1)
        run_id = self._automation_run(playlist_id)
        with patch.object(automation, "searchable_playlist_items", new=AsyncMock(return_value={
            "download_state": "known_empty", "items": [self._queue_item(playlist_id, item_ids[0])],
        })), \
             patch.object(automation, "begin_search_task_slot", side_effect=HTTPException(429, "容量已满")), \
             patch.object(automation, "library_details", new=AsyncMock(return_value=("not_found", None, None))):
            await automation.run_playlist_automation(run_id)
        with connect() as conn:
            run = conn.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(run["status"], "queued")
        self.assertIn("容量已满", run["message"] or "")

    async def test_failed_search_marks_run_partial_with_warning_notification(self) -> None:
        from app.services import automation
        playlist_id, item_ids = self._playlist_and_items(1)
        run_id = self._automation_run(playlist_id)

        async def fail_search(task_id: int) -> None:
            with connect() as conn:
                conn.execute("UPDATE search_tasks SET status='failed',completed=0 WHERE id=?", (task_id,))

        with patch.object(automation, "searchable_playlist_items", new=AsyncMock(return_value={
            "download_state": "known_empty", "items": [self._queue_item(playlist_id, item_ids[0])],
        })), \
             patch.object(automation, "run_search", new=fail_search), \
             patch.object(automation, "library_details", new=AsyncMock(return_value=("not_found", None, None))):
            await automation.run_playlist_automation(run_id)
        with connect() as conn:
            run = conn.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
            notification = conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(run["status"], "partial")
        self.assertIn("failed", run["message"] or "")
        self.assertEqual(notification["level"], "warning")
        self.assertIn("部分完成", notification["title"])

    async def test_partial_search_keeps_automation_run_partial_with_warning(self) -> None:
        """搜索任务 partial 不能被自动化流程误报为 completed。"""
        from app.services import automation
        playlist_id, item_ids = self._playlist_and_items(1)
        run_id = self._automation_run(playlist_id)

        async def partial_search(task_id: int) -> None:
            with connect() as conn:
                conn.execute(
                    "UPDATE search_tasks SET status='partial',completed=1,error_message=? WHERE id=?",
                    ("站点乙：连接超时 passkey=should-not-leak", task_id),
                )

        with patch.object(automation, "searchable_playlist_items", new=AsyncMock(return_value={
            "download_state": "known_empty", "items": [self._queue_item(playlist_id, item_ids[0])],
        })), \
             patch.object(automation, "run_search", new=partial_search), \
             patch.object(automation, "library_details", new=AsyncMock(return_value=("not_found", None, None))):
            await automation.run_playlist_automation(run_id)
        with connect() as conn:
            run = conn.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
            notification = conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["searched"], 1)
        self.assertIn("partial", run["message"] or "")
        self.assertEqual(notification["level"], "warning")
        self.assertNotIn("should-not-leak", run["message"] or "")

    async def test_successful_search_marks_run_completed_with_success_notification(self) -> None:
        from app.services import automation
        playlist_id, item_ids = self._playlist_and_items(1)
        run_id = self._automation_run(playlist_id)

        async def complete_search(task_id: int) -> None:
            with connect() as conn:
                conn.execute("UPDATE search_tasks SET status='completed',completed=1 WHERE id=?", (task_id,))

        with patch.object(automation, "searchable_playlist_items", new=AsyncMock(return_value={
            "download_state": "known_empty", "items": [self._queue_item(playlist_id, item_ids[0])],
        })), \
             patch.object(automation, "run_search", new=complete_search), \
             patch.object(automation, "library_details", new=AsyncMock(return_value=("not_found", None, None))):
            await automation.run_playlist_automation(run_id)
        with connect() as conn:
            run = conn.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
            notification = conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["searched"], 1)
        self.assertEqual(notification["level"], "success")
        self.assertIn("处理完成", notification["title"])

    async def test_scheduler_heartbeat_continues_during_long_tick(self) -> None:
        from app import state
        from app.services import automation

        heartbeat_count = 0
        heartbeat_seen = asyncio.Event()
        release_tick = asyncio.Event()

        def record_heartbeat(_timestamp: str) -> None:
            nonlocal heartbeat_count
            heartbeat_count += 1
            if heartbeat_count >= 3:
                heartbeat_seen.set()

        async def slow_account_refresh(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            await release_tick.wait()
            return []

        with patch.object(automation, "SCHEDULER_HEARTBEAT_INTERVAL_SECONDS", 0.001), \
             patch.object(state, "mark_scheduler_heartbeat", side_effect=record_heartbeat), \
             patch.object(automation, "cleanup_old_data", new=Mock()), \
             patch.object(automation, "refresh_stale_site_account_stats", new=slow_account_refresh), \
             patch.object(automation, "consume_queued_automation_runs", new=AsyncMock(return_value=0)):
            runner = asyncio.create_task(automation.sync_scheduler())
            await asyncio.wait_for(heartbeat_seen.wait(), timeout=1)
            self.assertGreaterEqual(heartbeat_count, 3)
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner
            release_tick.set()


class SchedulerHealthTests(IsolatedAppTestCase):
    def _restore_scheduler_state(self) -> None:
        # 测试会修改进程级 scheduler 状态，通过模块对象恢复默认值。
        import app.state as state

        state.scheduler_running = False
        state.scheduler_started_at = None
        state.scheduler_last_heartbeat_at = None
        state.scheduler_last_success_at = None
        state.scheduler_last_error = None
        state._scheduler_last_heartbeat_monotonic = None

    async def test_health_endpoint_exposes_scheduler_snapshot(self) -> None:
        with TestClient(app) as client:
            health = client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        scheduler = health.json()["scheduler"]
        self.assertIn("running", scheduler)
        self.assertIn("status", scheduler)
        self.assertIn("last_heartbeat_at", scheduler)
        self.assertIn("last_success_at", scheduler)
        self.assertIn("last_error", scheduler)
        self.assertEqual(health.json()["scheduler_ok"], scheduler["ok"])

    async def test_scheduler_health_reports_stopped_when_never_started(self) -> None:
        self._restore_scheduler_state()
        snapshot = scheduler_health()
        self.assertFalse(snapshot["ok"])
        self.assertEqual(snapshot["status"], "stopped")
        self.assertFalse(snapshot["running"])
        self.assertIsNone(snapshot["last_heartbeat_at"])
        self.assertIsNone(snapshot["last_error"])

    async def test_scheduler_health_tracks_heartbeat_success_and_staleness(self) -> None:
        self._restore_scheduler_state()
        mark_scheduler_started("2026-08-10T00:00:00Z")
        fresh = scheduler_health()
        self.assertTrue(fresh["ok"])
        self.assertEqual(fresh["status"], "starting")
        mark_scheduler_heartbeat("2026-08-10T00:01:00Z", monotonic_now=100.0)
        beating = scheduler_health(monotonic_now=110.0)
        self.assertEqual(beating["status"], "ok")
        self.assertTrue(beating["ok"])
        self.assertEqual(beating["last_heartbeat_at"], "2026-08-10T00:01:00Z")
        mark_scheduler_success("2026-08-10T00:02:00Z")
        successful = scheduler_health(monotonic_now=110.0)
        self.assertEqual(successful["last_success_at"], "2026-08-10T00:02:00Z")
        # 超过 180 秒未心跳 → stale
        stale = scheduler_health(monotonic_now=300.0)
        self.assertEqual(stale["status"], "stale")
        self.assertFalse(stale["ok"])


class ActiveTaskUniquenessTests(IsolatedAppTestCase):
    async def test_unique_partial_indexes_exist_after_initialize(self) -> None:
        with connect() as conn:
            indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for expected in (
            "idx_automation_active_playlist",
            "idx_recognition_active_playlist",
            "idx_library_scan_active_playlist",
        ):
            self.assertIn(expected, indexes)

    async def test_duplicate_active_automation_task_is_rejected_by_index(self) -> None:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("并发片单", 1, utc_now()),
            ).lastrowid)
            now = utc_now()
            conn.execute(
                """INSERT INTO automation_runs(playlist_id,status,trigger,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "queued", "manual", now, now),
            )
            with self.assertRaises(Exception) as raised:
                conn.execute(
                    """INSERT INTO automation_runs(playlist_id,status,trigger,created_at,updated_at)
                       VALUES(?,?,?,?,?)""",
                    (playlist_id, "running", "manual", now, now),
                )
        self.assertIn("UNIQUE", str(raised.exception).upper())

    async def test_initialize_retires_legacy_duplicate_active_tasks(self) -> None:
        # 模拟旧版本留下的重复 active 任务：先删掉唯一索引再插入两条。
        with connect() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_automation_active_playlist")
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("遗留片单", 1, utc_now()),
            ).lastrowid)
            now = utc_now()
            conn.execute(
                """INSERT INTO automation_runs(playlist_id,status,trigger,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "queued", "manual", now, now),
            )
            conn.execute(
                """INSERT INTO automation_runs(playlist_id,status,trigger,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (playlist_id, "running", "scheduler", now, now),
            )
            # 重复任务只会出现在旧版本（第 14 步之前）的数据库里。
            conn.execute("PRAGMA user_version=13")
        initialize()
        with connect() as conn:
            rows = conn.execute(
                "SELECT id,status,message FROM automation_runs WHERE playlist_id=? ORDER BY id",
                (playlist_id,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "interrupted")
        self.assertIn("重复", rows[0]["message"] or "")
        self.assertEqual(rows[1]["status"], "running")


class TaskCancelRouteTests(IsolatedAppTestCase):
    """2-18/2-19：取消任务、通知与设置测试路由的最小行为测试。"""

    async def _seed_search_task(self, status: str = "queued") -> int:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("路由覆盖片单", 1, utc_now()),
            ).lastrowid)
            return to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,trigger,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, status, 1, "manual", utc_now(), utc_now()),
            ).lastrowid)

    async def test_cancel_task_rejects_unknown_and_finished(self) -> None:
        with TestClient(app) as client:
            missing = client.post("/api/search-tasks/999999/cancel")
            self.assertEqual(missing.status_code, 404)
            finished_id = await self._seed_search_task("completed")
            finished = client.post(f"/api/search-tasks/{finished_id}/cancel")
            self.assertEqual(finished.status_code, 409)

    async def test_cancel_task_cancels_running_db_task(self) -> None:
        # 任务须在 TestClient（lifespan）启动之后再插入：启动时会把存量
        # queued/running 任务置为 interrupted。
        with TestClient(app) as client:
            task_id = await self._seed_search_task("running")
            response = client.post(f"/api/search-tasks/{task_id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        with connect() as conn:
            row = conn.execute("SELECT status FROM search_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(row["status"], "cancelled")
