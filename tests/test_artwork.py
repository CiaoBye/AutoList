"""海报缓存：磁盘副本、并发下载合并与预热。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import state
from app.config import settings
from app.database import connect
from app.services import artwork
from app.util import utc_now
from tests.support import IsolatedAppTestCase


class PosterCacheTests(IsolatedAppTestCase):
    async def test_a_poster_survives_a_restart_through_the_disk_copy(self) -> None:
        state.remember_poster("fanart:a", (b"\x89PNG-data", "image/png"))
        # 模拟重启：进程内缓存清空，磁盘副本仍在。
        state.poster_cache.clear()
        self.assertIn("fanart:a", state.poster_cache)
        self.assertEqual(state.poster_cache.get("fanart:a"), (b"\x89PNG-data", "image/png"))
        self.assertEqual(state.poster_cache["fanart:a"][1], "image/png")
        self.assertIsNone(state.poster_cache.get("missing"))
        with self.assertRaises(KeyError):
            state.poster_cache["missing"]

    async def test_old_disk_files_are_pruned_beyond_the_limit(self) -> None:
        with patch.object(state, "MAX_DISK_FILES", 3), patch.object(state.PosterCache, "DISK_PRUNE_EVERY", 1):
            for index in range(6):
                state.remember_poster(f"k{index}", (b"x" * 10, "image/jpeg"))
        files = [item for item in (Path(settings.data_dir) / "cache" / "images").glob("*/*") if item.is_file()]
        self.assertLessEqual(len(files), 4)

    async def test_concurrent_downloads_of_one_poster_share_a_single_fetch(self) -> None:
        calls = 0

        async def fetch() -> tuple[bytes, str]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return b"img", "image/jpeg"

        results = await asyncio.gather(*(state.fetch_once("same", fetch) for _ in range(5)))
        self.assertEqual(calls, 1)
        self.assertEqual(results, [(b"img", "image/jpeg")] * 5)
        # 失败也会同时传给所有等待者，之后可以重新尝试。
        async def broken() -> tuple[bytes, str]:
            await asyncio.sleep(0.01)
            raise RuntimeError("上游失败")

        outcomes = await asyncio.gather(*(state.fetch_once("bad", broken) for _ in range(3)), return_exceptions=True)
        self.assertTrue(all(isinstance(item, RuntimeError) for item in outcomes))
        self.assertEqual(await state.fetch_once("bad", fetch), (b"img", "image/jpeg"))


class PosterWarmTests(IsolatedAppTestCase):
    async def test_warming_requests_every_recognised_film_and_tolerates_failures(self) -> None:
        with connect() as conn:
            playlist = conn.execute("INSERT INTO playlists(name,created_at) VALUES('P',?)", (utc_now(),)).lastrowid
            for rank, tmdb in ((1, 10), (2, None), (3, 30)):
                conn.execute(
                    "INSERT INTO playlist_items(playlist_id,rank_no,original_title,tmdb_id,library_state) VALUES(?,?,?,?,'not_found')",
                    (playlist, rank, f"F{rank}", tmdb),
                )
        calls: list[int] = []

        async def fake(item_id: int, v: str = "") -> None:
            calls.append(item_id)
            if len(calls) == 1:
                raise RuntimeError("上游超时")

        with patch("app.api.images.playlist_item_fanart_poster", new=fake):
            count = await artwork.warm_posters()
        self.assertEqual((count, len(calls)), (2, 2))
        self.assertFalse(artwork.warm_due())
