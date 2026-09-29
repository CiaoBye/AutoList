"""共享测试隔离基类（审计 2-24 / 3-17）。

统一管理临时数据目录、Settings 快照恢复与进程级缓存清理，
避免各测试文件重复实现互不相同的隔离样板。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import state, tasks
from app.config import settings
from app.database import connect, initialize
from app.util import to_int, utc_now


def task_candidates(task_id: int) -> list[dict]:
    """某次寻片涉及影片的当前候选（与影片详情、挑选页同一套聚合），按片单顺序。"""
    from app.services.candidates import latest_candidate_rows
    from app.services.candidates import present_candidates
    from app.util import rows_to_dicts

    with connect() as conn:
        item_ids = [row[0] for row in conn.execute(
            """SELECT DISTINCT c.playlist_item_id FROM candidates c JOIN playlist_items p ON p.id=c.playlist_item_id
               WHERE c.task_id=? ORDER BY p.rank_no""", (task_id,),
        ).fetchall()]
        rows = latest_candidate_rows(conn, item_ids)
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
        state.raw_candidates.clear()
        initialize()

    async def asyncTearDown(self) -> None:
        # 导入、识别等接口会启动后台任务；必须在删除临时数据目录前取消并等待，
        # 否则任务会在目录删除后继续写库并输出 “unable to open database file”。
        background = [task for kind in tasks.KINDS for task in list(kind.running.values()) if task and not task.done()]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        for kind in tasks.KINDS:
            kind.running.clear()
        state.raw_candidates.clear()
        # 进程级缓存与限流时间戳必须清理，避免跨测试假阳性（审计 2-24）。
        state.poster_cache.clear()
        state.site_icon_cache.clear()
        from app.services import cookiecloud as cookiecloud_module
        cookiecloud_module.reset_pull_clock()
        from app.services import search as search_module
        search_module._transmission_snapshot_cache.clear()
        search_module._site_request_times.clear()
        search_module._site_rate_locks.clear()
        from app.clients import close_search_clients
        await close_search_clients()
        import app.outbound as outbound_module
        outbound_module._dns_address_cache.clear()
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


class SeededPlaylistTestCase(IsolatedAppTestCase):
    """预置一个 250 部影片的片单：单号已入馆，双号入馆状态未知。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        settings.dashboard_random_posters = False
        with connect() as conn:
            self.playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)", ("测试片单", 1, utc_now()),
            ).lastrowid)
            conn.executemany(
                """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,library_state)
                   VALUES(?,?,?,?,?,?,?)""",
                [
                    (self.playlist_id, index, f"tt{index:07d}", f"Movie {index}", 2000 + index % 20, f"电影 {index}", "in_library" if index % 2 else "unknown")
                    for index in range(1, 251)
                ],
            )


class FilmFixture(IsolatedAppTestCase):
    """A playlist whose items cover every film status and issue."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        now = utc_now()
        with connect() as conn:
            self.playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)", ("测试片单", 1, now),
            ).lastrowid)
            self.site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at) VALUES(?,?,?,?,?,?)",
                ("春天", "nexusphp", "https://site.example", 1, 1, now),
            ).lastrowid)
            self.items: dict[str, int] = {}
            specs = [
                ("in_library", 1, 101, "in_library"),
                ("missing", 2, 102, "not_found"),
                ("unchecked", 3, 103, "unknown"),
                ("unrecognized", 4, None, "not_found"),
                ("candidates", 5, 105, "not_found"),
                ("selected", 6, 106, "not_found"),
                ("downloading", 7, 107, "not_found"),
                ("no_eligible", 8, 108, "not_found"),
                ("submit_failed", 9, 109, "not_found"),
                ("strm", 10, 110, "strm"),
            ]
            for key, rank, tmdb_id, library_state in specs:
                self.items[key] = to_int(conn.execute(
                    """INSERT INTO playlist_items(
                         playlist_id,rank_no,original_title,year,tmdb_id,tmdb_title,tmdb_original_title,tmdb_year,
                         library_state,library_checked_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (self.playlist_id, rank, f"Movie {key}", 2000 + rank, tmdb_id,
                     f"电影{key}" if tmdb_id else None, f"Movie {key}" if tmdb_id else None,
                     2000 + rank if tmdb_id else None, library_state, now),
                ).lastrowid)
            self.task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,trigger,item_ids_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.playlist_id, 5, 8, "completed", 3, "manual",
                 json.dumps([self.items["candidates"], self.items["selected"], self.items["no_eligible"]]), now, now),
            ).lastrowid)
            for key in ("candidates", "selected", "no_eligible"):
                conn.execute(
                    """INSERT INTO search_attempts(task_id,playlist_item_id,site_id,site_name,status,result_count,duration_ms,created_at,finished_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (self.task_id, self.items[key], self.site_id, "春天", "success", 2, 500, now, now),
                )
            self.candidate_ids = {
                "candidates": self._candidate(conn, "candidates", "Movie.candidates.2005.1080p.BluRay.x265-CHD", "eligible"),
                "selected": self._candidate(conn, "selected", "Movie.selected.2006.1080p.BluRay.x265-CHD", "eligible"),
                "excluded": self._candidate(conn, "no_eligible", "Movie.no_eligible.2008.1080p.WEB-DL", "excluded", "WEB-DL 已排除"),
            }
            conn.execute("INSERT INTO selection_items(candidate_id,selected_at) VALUES(?,?)", (self.candidate_ids["selected"], now))
            conn.execute(
                """INSERT INTO download_history(playlist_item_id,title,torrent_name,site_name,submission_hash,success,message,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (self.items["downloading"], "电影downloading", "Movie.downloading.2007.1080p.x265-CHD", "春天", "hash7", 1, "ok", now),
            )
            conn.execute(
                """INSERT INTO download_history(playlist_item_id,title,torrent_name,site_name,success,message,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.items["submit_failed"], "电影submit_failed", "Movie.submit_failed.2009", "春天", 0,
                 "失败：https://site.example/download.php?passkey=SECRET_PASSKEY", now),
            )
        state.raw_candidates[self.candidate_ids["candidates"]] = {"media": {}, "torrent": {}}

    def _candidate(self, conn, key: str, title: str, eligibility: str, reason: str | None = None) -> str:
        candidate_id = f"cand-{key}-{eligibility}"
        conn.execute(
            """INSERT INTO candidates(
                 id,task_id,playlist_item_id,candidate_index,title,site_name,size,seeders,ranking,recommendation,
                 eligibility,exclusion_reason,resource_key,metadata_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (candidate_id, self.task_id, self.items[key], 0, title, "春天", 9_000_000_000, 12, 1,
             "preferred" if eligibility == "eligible" else "excluded", eligibility, reason, title, "{}", utc_now()),
        )
        return candidate_id

    def _no_transmission(self):
        return patch("app.services.films._current_downloads_cached", new=AsyncMock(return_value=([], "known_empty")))

    async def _films(self, **params):
        defaults = {"playlist_id": self.playlist_id, "status": "all", "q": "", "page": 1, "page_size": 60}
        from app.api import films as film_routes

        with self._no_transmission():
            return await film_routes.films(**{**defaults, **params})
