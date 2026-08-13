"""共享测试隔离基类（审计 2-24 / 3-17）。

统一管理临时数据目录、Settings 快照恢复与进程级缓存清理，
避免各测试文件重复实现互不相同的隔离样板。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from app.compat import main  # noqa: E402 (审计 2-12)
from app import state
from app.config import settings
from app.database import initialize


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
        main.raw_candidates.clear()
        # 进程级缓存与限流时间戳必须清理，避免跨测试假阳性（审计 2-24）。
        state.poster_cache.clear()
        state.site_icon_cache.clear()
        state._cookiecloud_upload_times.clear()
        state._cookiecloud_get_times.clear()
        from app.services import search as search_module
        search_module._transmission_snapshot_cache.clear()
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
