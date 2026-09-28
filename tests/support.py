"""共享测试隔离基类（审计 2-24 / 3-17）。

统一管理临时数据目录、Settings 快照恢复与进程级缓存清理，
避免各测试文件重复实现互不相同的隔离样板。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from app.compat import main  # noqa: E402 (审计 2-12)
from app import state
from app.config import settings
from app.database import connect, initialize


def task_candidates(task_id: int) -> list[dict]:
    """某次寻片涉及影片的当前候选（与影片详情、挑选页同一套聚合），按片单顺序。"""
    from app.api.films import _latest_candidate_rows
    from app.services.candidates import present_candidates
    from app.util import rows_to_dicts

    with connect() as conn:
        item_ids = [row[0] for row in conn.execute(
            """SELECT DISTINCT c.playlist_item_id FROM candidates c JOIN playlist_items p ON p.id=c.playlist_item_id
               WHERE c.task_id=? ORDER BY p.rank_no""", (task_id,),
        ).fetchall()]
        rows = _latest_candidate_rows(conn, item_ids)
        sites = rows_to_dicts(conn.execute("SELECT name,priority,icon_url FROM pt_sites").fetchall())
    return [candidate for item_id in item_ids for candidate in present_candidates(rows.get(item_id, []), sites)]


class IsolatedAppTestCase(unittest.IsolatedAsyncioTestCase):
    """每个测试使用独立临时数据目录，并在结束后恢复 Settings 与进程级缓存。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self._previous_data_dir = settings.data_dir
        self._previous_token = os.environ.get("AUTOLIST_ACCESS_TOKEN")
        self._previous_strict = os.environ.get("AUTOLIST_REQUIRE_STRONG_TOKEN")
        self._settings_snapshot = dict(vars(settings))
        settings.data_dir = self.temp.name
        os.environ.pop("AUTOLIST_ACCESS_TOKEN", None)
        os.environ.pop("AUTOLIST_REQUIRE_STRONG_TOKEN", None)
        main.raw_candidates.clear()
        initialize()

    async def asyncTearDown(self) -> None:
        # 导入、识别等接口会启动后台任务；必须在删除临时数据目录前取消并等待，
        # 否则任务会在目录删除后继续写库并输出 “unable to open database file”。
        background = [
            task
            for registry in (
                state.running_tasks, state.running_recognition_tasks,
                state.running_library_tasks, state.running_automation_tasks,
            )
            for task in list(registry.values())
            if task and not task.done()
        ]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        for registry in (
            state.running_tasks, state.running_recognition_tasks,
            state.running_library_tasks, state.running_automation_tasks,
        ):
            registry.clear()
        main.raw_candidates.clear()
        # 进程级缓存与限流时间戳必须清理，避免跨测试假阳性（审计 2-24）。
        state.poster_cache.clear()
        state.site_icon_cache.clear()
        state._cookiecloud_upload_times.clear()
        state._cookiecloud_get_times.clear()
        from app.services import search as search_module
        search_module._transmission_snapshot_cache.clear()
        search_module._site_request_times.clear()
        search_module._site_rate_locks.clear()
        from app.clients import close_search_clients
        await close_search_clients()
        import app.util as util_module
        util_module._dns_address_cache.clear()
        from app.api import system as system_module
        system_module._connection_cache = None
        settings.__dict__.clear()
        settings.__dict__.update(self._settings_snapshot)
        settings.data_dir = self._previous_data_dir
        if self._previous_token is None:
            os.environ.pop("AUTOLIST_ACCESS_TOKEN", None)
        else:
            os.environ["AUTOLIST_ACCESS_TOKEN"] = self._previous_token
        if self._previous_strict is None:
            os.environ.pop("AUTOLIST_REQUIRE_STRONG_TOKEN", None)
        else:
            os.environ["AUTOLIST_REQUIRE_STRONG_TOKEN"] = self._previous_strict
        self.temp.cleanup()
