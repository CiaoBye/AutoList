"""提交：待入馆清单、候选上下文、经 MoviePilot 提交与提交记录。"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app.api import history as history_routes
from app.api.search import task_attempts, task_logs, task_status
from app.api.selection import _matches_active_torrent, selection, submit_selection, toggle_selection
from app.clients import MoviePilotClient, TransmissionClient
from app.config import settings
from app.database import connect, json_value
from app.main import app
from app.services.history import clear_download_history, projected_download_history
from app.services.search import create_followup_search_task, run_search, searchable_playlist_items
from app.state import raw_candidates
from app.util import to_int, utc_now
from tests.support import IsolatedAppTestCase, SeededPlaylistTestCase, task_candidates


class MoviePilotGatewayTests(unittest.TestCase):
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
        route_paths = {route.path for route in app.routes}
        self.assertNotIn("/api/sites/moviepilot-enhancements", route_paths)
        self.assertNotIn("/api/config/release-groups/import-moviepilot", route_paths)
        self.assertNotIn("cookiecloud_forward_moviepilot", settings.public_values())


class SelectionApiTests(SeededPlaylistTestCase):
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
                    (items[1]["id"], "Movie 1", "Movie 1 2001 1080p", "站点一", "hash-1", 1, None, utc_now()),
                    (items[2]["id"], "Movie 2", "Movie 2 2002 1080p", "站点二", "hash-2", 1, None, utc_now()),
                    (items[3]["id"], "Movie 3", "Movie 3 2003 1080p", "站点三", "hash-3", 1, None, utc_now()),
                    (items[4]["id"], "Movie 4", "Movie 4 2004 1080p", "站点四", "hash-4", 0, "提交失败 passkey=secret", utc_now()),
                    (items[5]["id"], "Movie 5", "Movie 5 2005 1080p", "站点五", "hash-5", 1, None, utc_now()),
                ],
            )
        torrents = [{
            "id": 22, "name": "Movie 2 2002 1080p", "hashString": "hash-2", "status": 4,
            "percentDone": 0.24, "labels": ["MOVIEPILOT", "站点二"], "downloadDir": "/Media/Raw/Film",
        }]
        with patch.object(TransmissionClient, "current_downloads", new=AsyncMock(return_value=torrents)):
            result = await history_routes.history()
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

    async def test_expired_selection_context_is_visible_and_recorded_safely(self) -> None:
        with connect() as conn:
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,size,group_tier,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("expired", task_id, item_id, 0, "Example 1080p", "Test", 1024, 9, 1, "{}", utc_now()),
            )
            conn.execute("INSERT INTO selection_items(candidate_id,selected_at) VALUES(?,?)", ("expired", utc_now()))
        items = await selection()
        self.assertFalse(items[0]["context_available"])
        with self.assertRaises(HTTPException) as raised:
            await submit_selection()
        self.assertEqual(raised.exception.status_code, 409)
        with connect() as conn:
            history = conn.execute("SELECT success,message FROM download_history WHERE candidate_id='expired'").fetchall()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["success"], 0)
        self.assertNotIn("http", history[0]["message"].lower())

    async def test_concurrent_selection_submission_calls_moviepilot_once(self) -> None:
        with connect() as conn:
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO candidates(
                     id,task_id,playlist_item_id,candidate_index,title,site_name,ranking,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("concurrent", task_id, item_id, 0, "Movie.1.1080p", "Test", 1, "{}", utc_now()),
            )
            conn.execute("INSERT INTO selection_items(candidate_id,selected_at) VALUES(?,?)", ("concurrent", utc_now()))
        raw_candidates["concurrent"] = {
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

        with patch.object(MoviePilotClient, "download", new=delayed_download):
            first = asyncio.create_task(submit_selection())
            await entered.wait()
            with self.assertRaises(HTTPException) as raised:
                await submit_selection()
            result = await first
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(calls, 1)

    async def test_history_clear_only_deletes_selected_lifecycle_group(self) -> None:
        with connect() as conn:
            conn.executemany(
                """INSERT INTO download_history(title,torrent_name,success,message,created_at)
                   VALUES(?,?,?,?,?)""",
                [
                    ("失败影片", "失败资源", 0, "提交失败", utc_now()),
                    ("成功影片", "成功资源", 1, None, utc_now()),
                ],
            )
        with patch.object(TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])):
            deleted = await clear_download_history("failed")
        with connect() as conn:
            rows = conn.execute("SELECT title,success FROM download_history ORDER BY id").fetchall()
        self.assertEqual(deleted, 1)
        self.assertEqual([(row["title"], row["success"]) for row in rows], [("成功影片", 1)])

    async def test_selection_rejects_duplicate_release_and_submit_skips_submitted(self) -> None:
        """同影片同站点同发布只能加入待入馆清单一次；已成功提交过的发布再次提交会被跳过。"""
        with connect() as conn:
            task_id = conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (self.playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid
            item_id = to_int(conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0])
            now = utc_now()
            for candidate_id in ("dup-a", "dup-b"):
                conn.execute(
                    """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,size,eligibility,resource_key,ranking,metadata_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (candidate_id, task_id, item_id, 0, "Movie.2020.1080p.x265-FRDS", "Alpha", 8 * 1024**3,
                     "eligible", "movie20201080px265frds:128", 1, "{}", now),
                )
        raw_candidates["dup-a"] = {"media": {"id": 1}, "torrent": {"title": "t"}}
        raw_candidates["dup-b"] = {"media": {"id": 1}, "torrent": {"title": "t"}}
        await toggle_selection("dup-a")
        with self.assertRaises(HTTPException) as raised:
            await toggle_selection("dup-b")
        self.assertEqual(raised.exception.status_code, 422)
        # 已有成功提交记录时，再次提交同一发布应跳过而非重复下载。
        with connect() as conn:
            conn.execute(
                """INSERT INTO download_history(candidate_id,playlist_item_id,title,torrent_name,site_name,success,created_at)
                   VALUES(?,?,?,?,?,1,?)""",
                ("dup-a", item_id, "Movie 1", "Movie.2020.1080p.x265-FRDS", "Alpha", utc_now()),
            )
        with patch.object(MoviePilotClient, "download", new=AsyncMock()) as download_mock, \
             patch.object(TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])):
            result = await submit_selection()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["reason"], "该发布已提交过")
        download_mock.assert_not_awaited()

    async def test_resource_key_history_blocks_duplicate_after_candidate_cleanup(self) -> None:
        resource_key = "movie2020release:128"
        with connect() as conn:
            task_id = to_int(conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (self.playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid)
            item_id = to_int(conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0])
            conn.execute(
                """INSERT INTO candidates(
                     id,task_id,playlist_item_id,candidate_index,title,site_name,size,eligibility,resource_key,ranking,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("old-release", task_id, item_id, 0, "Movie.2020.1080p", "Alpha", 8 * 1024**3, "eligible", resource_key, 1, "{}", utc_now()),
            )
            conn.execute(
                """INSERT INTO download_history(
                     candidate_id,playlist_item_id,resource_key,title,torrent_name,site_name,success,created_at
                   ) VALUES(?,?,?,?,?,?,1,?)""",
                ("old-release", item_id, resource_key, "Movie 1", "Movie.2020.1080p", "Alpha", utc_now()),
            )
            # 模拟 30 天候选清理：历史快照必须独立保留去重 key。
            conn.execute("DELETE FROM candidates WHERE id='old-release'")
            conn.execute(
                """INSERT INTO candidates(
                     id,task_id,playlist_item_id,candidate_index,title,site_name,size,eligibility,resource_key,ranking,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("new-release", task_id, item_id, 0, "Movie.2020.1080p", "Alpha", 8 * 1024**3, "eligible", resource_key, 1, "{}", utc_now()),
            )
            conn.execute("INSERT INTO selection_items(candidate_id,selected_at) VALUES(?,?)", ("new-release", utc_now()))
        raw_candidates["new-release"] = {"media": {"id": 1}, "torrent": {"title": "Movie.2020.1080p"}}
        with patch.object(MoviePilotClient, "download", new=AsyncMock()) as download_mock, \
             patch.object(TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])):
            result = await submit_selection()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "该发布已提交过")
        download_mock.assert_not_awaited()

    async def test_submit_selection_skips_release_already_downloading(self) -> None:
        """Transmission 已有同名活动任务时，提交自动跳过。"""
        with connect() as conn:
            task_id = conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (self.playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid
            item_id = to_int(conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=1", (self.playlist_id,),
            ).fetchone()[0])
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,eligibility,resource_key,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("downloading-candidate", task_id, item_id, 0, "Movie.2020.1080p.x265-FRDS", "Alpha",
                 "eligible", "movie20201080px265frds:128", 1, "{}", utc_now()),
            )
            conn.execute("INSERT INTO selection_items(candidate_id,selected_at) VALUES(?,?)", ("downloading-candidate", utc_now()))
        raw_candidates["downloading-candidate"] = {"media": {"id": 1}, "torrent": {"title": "Movie.2020.1080p.x265-FRDS"}}
        with patch.object(MoviePilotClient, "download", new=AsyncMock()) as download_mock, \
             patch.object(TransmissionClient, "current_downloads", new=AsyncMock(return_value=[
                 {"name": "Movie.2020.1080p.x265-FRDS", "status": 4, "percentDone": 0.3},
             ])):
            result = await submit_selection()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "Transmission 正在下载")
        download_mock.assert_not_awaited()


class SubmissionWorkflowTests(IsolatedAppTestCase):
    def create_playlist_item(self, title: str = "Workflow Movie", year: int = 2020) -> tuple[int, int]:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("工作流测试", 1, utc_now()),
            ).lastrowid)
            item_id = to_int(conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title)
                   VALUES(?,?,?,?,?,?)""",
                (playlist_id, 1, "tt1234567", title, year, "工作流电影"),
            ).lastrowid)
        return playlist_id, item_id

    async def _selected_site_candidate(self) -> str:
        """寻片得到一个带详情页的站点候选并加入清单，随后站点 Cookie 被 CookieCloud 更新。"""
        playlist_id, _item_id = self.create_playlist_item()
        with connect() as conn:
            conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,cookie,user_agent,enabled,search_enabled,created_at)
                   VALUES(?,?,?,?,?,1,1,?)""",
                ("Alpha", "nexusphp", "https://alpha.example", "old-cookie", "UA/1", utc_now()),
            )
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, "queued", 1, utc_now(), utc_now()),
            ).lastrowid)
        media = {"id": 321, "title": "Workflow Movie", "original_title": "Workflow Movie", "release_date": "2020-01-01"}

        async def search(_client: object, site: dict, _title: str, _imdb: str | None = None) -> list[dict]:
            return [{
                "title": "Workflow.Movie.2020.2160p.BluRay.x265-FRDS", "size": 8 * 1024**3, "seeders": 20,
                "site_name": site["name"], "enclosure": "https://alpha.example/download.php?id=7&passkey=super-secret",
                "detail_url": "https://alpha.example/details.php?id=7", "labels": [],
                "site_cookie": site["cookie"], "site_ua": site["user_agent"],
            }]

        with patch("app.services.search.recognize_item", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.NexusPHPClient.search", new=search):
            await run_search(task_id)
        candidate_id = task_candidates(task_id)[0]["id"]
        self.assertTrue((await toggle_selection(candidate_id))["in_selection"])
        with connect() as conn:
            conn.execute("UPDATE pt_sites SET cookie=? WHERE name=?", ("fresh-cookie", "Alpha"))
        return candidate_id

    async def test_mocked_search_aggregates_sites_and_submits_through_moviepilot(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        with connect() as conn:
            for name, priority in (("Alpha", 1), ("Beta", 2), ("Broken", 3)):
                conn.execute(
                    """INSERT INTO pt_sites(name,adapter,base_url,priority,enabled,search_enabled,created_at)
                       VALUES(?,?,?,?,1,1,?)""",
                    (name, "nexusphp", f"https://{name.lower()}.example", priority, utc_now()),
                )
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, "queued", 1, utc_now(), utc_now()),
            ).lastrowid)

        media = {"id": 321, "title": "Workflow Movie", "original_title": "Workflow Movie", "release_date": "2020-01-01"}
        base_torrent = {
            "title": "Workflow.Movie.2020.2160p.BluRay.x265-FRDS",
            "size": 8 * 1024**3,
            "seeders": 20,
            "enclosure": "https://tracker.example/download?passkey=super-secret",
        }

        async def search(_client: object, site: dict, _title: str, _imdb: str | None = None) -> list[dict]:
            if site["name"] == "Broken":
                raise RuntimeError("https://broken.example/?passkey=should-not-leak")
            return [{
                **base_torrent,
                "site_name": site["name"],
                "labels": ["FREE"] if site["name"] == "Beta" else [],
                "volume_factor": 0 if site["name"] == "Beta" else 1,
            }]

        with patch("app.services.search.recognize_item", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.NexusPHPClient.search", new=search):
            await run_search(task_id)

        task = await task_status(task_id)
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["completed"], 1)
        self.assertEqual(task["matched"], 1)
        grouped = task_candidates(task_id)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["site_count"], 2)
        self.assertEqual(grouped[0]["site_name"], "Alpha")
        self.assertEqual([option["site_name"] for option in grouped[0]["site_options"]], ["Alpha", "Beta"])
        logs = await task_logs(task_id)
        self.assertTrue(any(log["level"] == "warning" and "Broken" in log["message"] for log in logs))
        self.assertFalse(any("should-not-leak" in log["message"] for log in logs))
        attempts = await task_attempts(task_id)
        self.assertEqual(len(attempts["items"]), 3)
        self.assertEqual(sum(to_int(site["failed"] or 0) for site in attempts["sites"]), 1)
        retry_id, retry_total = create_followup_search_task(task_id, True)
        self.assertEqual(retry_total, 1)
        with connect() as conn:
            retry = conn.execute("SELECT parent_task_id,trigger,site_ids_json,item_ids_json FROM search_tasks WHERE id=?", (retry_id,)).fetchone()
        self.assertEqual(retry["parent_task_id"], task_id)
        self.assertEqual(retry["trigger"], "retry")
        self.assertEqual(len(json.loads(retry["site_ids_json"])), 1)
        self.assertEqual(json.loads(retry["item_ids_json"]), [item_id])

        restart_id, restart_total = create_followup_search_task(task_id, False)
        self.assertEqual(restart_total, 1)
        with connect() as conn:
            restart = conn.execute("SELECT parent_task_id,trigger,item_ids_json,site_ids_json FROM search_tasks WHERE id=?", (restart_id,)).fetchone()
        self.assertEqual(restart["parent_task_id"], task_id)
        self.assertEqual(restart["trigger"], "restart")
        self.assertEqual(restart["item_ids_json"], None)
        self.assertEqual(restart["site_ids_json"], None)

        candidate_id = grouped[0]["id"]
        toggled = await toggle_selection(candidate_id)
        self.assertTrue(toggled["in_selection"])
        submitted: list[tuple[dict, dict, str | None]] = []

        async def download(_client: object, media_in: dict, torrent_in: dict, downloader: str | None = None) -> dict:
            submitted.append((media_in, torrent_in, downloader))
            return {"success": True, "hash": "safe-hash"}

        with patch.object(MoviePilotClient, "download", new=download):
            result = await submit_selection()
        self.assertEqual(result["mode"], "moviepilot")
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(submitted[0][2], "Transmission")
        self.assertEqual(submitted[0][0]["type"], "电影")
        self.assertEqual(submitted[0][0]["source"], "themoviedb")
        self.assertIsInstance(submitted[0][0]["year"], str)
        self.assertIn("passkey=super-secret", submitted[0][1]["enclosure"])
        self.assertEqual(await selection(), [])
        history = await history_routes.history()
        self.assertTrue(history[0]["success"])
        self.assertNotIn("super-secret", json.dumps(history, ensure_ascii=False))

    async def test_candidate_context_is_encrypted_without_site_cookie(self) -> None:
        candidate_id = await self._selected_site_candidate()
        with connect() as conn:
            payload = bytes(conn.execute("SELECT payload FROM candidate_contexts WHERE candidate_id=?", (candidate_id,)).fetchone()[0])
        self.assertNotIn(b"super-secret", payload)
        self.assertNotIn(b"alpha.example", payload)
        context = raw_candidates[candidate_id]
        self.assertIn("passkey=super-secret", context["torrent"]["enclosure"])
        self.assertNotIn("site_cookie", context["torrent"])
        key_file = Path(settings.data_dir) / "candidate-context.key"
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        # 密钥被替换后旧上下文无法解密，按已过期处理；权限被放宽时读取会重新收紧。
        key_file.write_bytes(b"k" * 32)
        key_file.chmod(0o644)
        self.assertNotIn(candidate_id, raw_candidates.keys())
        with self.assertRaises(KeyError):
            raw_candidates[candidate_id]
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)

    async def test_submit_confirms_torrent_on_site_and_uses_current_cookie(self) -> None:
        candidate_id = await self._selected_site_candidate()
        submitted: list[dict] = []

        async def download(_client: object, _media: dict, torrent_in: dict, downloader: str | None = None) -> dict:
            submitted.append(torrent_in)
            return {"success": True, "hash": "safe-hash"}

        verify = AsyncMock(return_value=True)
        with patch("app.api.selection.verify_on_site", new=verify), \
             patch("app.api.selection.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch.object(MoviePilotClient, "download", new=download):
            result = await submit_selection()
        self.assertEqual(result["submitted"], 1)
        site, detail_url = verify.await_args.args
        self.assertEqual((site["name"], site["cookie"], detail_url), ("Alpha", "fresh-cookie", "https://alpha.example/details.php?id=7"))
        self.assertEqual((submitted[0]["site_cookie"], submitted[0]["site_ua"]), ("fresh-cookie", "UA/1"))
        self.assertNotIn(candidate_id, raw_candidates)

    async def test_submit_removes_torrents_deleted_by_the_site(self) -> None:
        candidate_id = await self._selected_site_candidate()
        download = AsyncMock()
        with patch("app.api.selection.verify_on_site", new=AsyncMock(return_value=False)), \
             patch("app.api.selection.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch.object(MoviePilotClient, "download", new=download):
            result = await submit_selection()
        download.assert_not_awaited()
        self.assertEqual((result["submitted"], [item["candidate_id"] for item in result["removed"]]), (0, [candidate_id]))
        self.assertEqual(await selection(), [])
        with connect() as conn:
            row = conn.execute("SELECT eligibility,exclusion_reason FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        self.assertEqual((row["eligibility"], row["exclusion_reason"]), ("excluded", "种子已被站点删除"))
        self.assertNotIn(candidate_id, raw_candidates)

    async def test_submit_holds_candidates_the_site_cannot_confirm(self) -> None:
        from app.sites.errors import CookieExpired

        candidate_id = await self._selected_site_candidate()
        download = AsyncMock()
        with patch("app.api.selection.verify_on_site", new=AsyncMock(side_effect=CookieExpired())), \
             patch("app.api.selection.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch.object(MoviePilotClient, "download", new=download):
            result = await submit_selection()
        download.assert_not_awaited()
        self.assertEqual(result["blocked_site"][0]["candidate_id"], candidate_id)
        self.assertIn("Cookie 已失效", result["blocked_site"][0]["reason"])
        self.assertEqual([item["id"] for item in await selection()], [candidate_id])
        self.assertIn(candidate_id, raw_candidates)

    async def test_moviepilot_false_response_is_recorded_as_failure(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        with connect() as conn:
            task_id = to_int(conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid)
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("false-response", task_id, item_id, 0, "Workflow.Movie.1080p", "Test", 1, "{}", utc_now()),
            )
            conn.execute("INSERT INTO selection_items(candidate_id,selected_at) VALUES(?,?)", ("false-response", utc_now()))
        raw_candidates["false-response"] = {
            "media": {"id": 1, "title": "Workflow Movie", "type": "电影"},
            "torrent": {"title": "Workflow.Movie.1080p"},
        }
        with patch.object(MoviePilotClient, "download", new=AsyncMock(return_value={"success": "false", "message": "下游拒绝"})):
            result = await submit_selection()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual((await selection())[0]["id"], "false-response")
        with connect() as conn:
            history = conn.execute("SELECT success,message FROM download_history WHERE candidate_id=?", ("false-response",)).fetchone()
        self.assertEqual(history["success"], 0)
        self.assertIn("下游拒绝", history["message"])

    async def test_excluded_candidate_cannot_enter_selection(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        candidate_id = "excluded-candidate"
        with connect() as conn:
            task_id = conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,eligibility,exclusion_reason,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, task_id, item_id, 0, "Movie.REMUX", "excluded", "包含 REMUX", 1, "{}", utc_now()),
            )
        raw_candidates[candidate_id] = {"media": {}, "torrent": {}}
        with self.assertRaises(HTTPException) as raised:
            await toggle_selection(candidate_id)
        self.assertEqual(raised.exception.status_code, 422)

    async def test_moviepilot_payload_maps_internal_torrent_fields(self) -> None:
        response = Mock()
        response.content = b"{}"
        response.raise_for_status = Mock()
        response.json.return_value = {"success": True}
        client = AsyncMock()
        client.post.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch.object(MoviePilotClient, "_client", return_value=context):
            await MoviePilotClient().download(
                {"year": "1982"},
                {"title": "Movie", "volume_factor": 0.5, "publish_time": "2026-07-18"},
                downloader="Transmission",
            )
        payload = client.post.await_args.kwargs["json"]
        self.assertNotIn("volume_factor", payload["torrent_in"])
        self.assertNotIn("publish_time", payload["torrent_in"])
        self.assertEqual(payload["torrent_in"]["downloadvolumefactor"], 0.5)
        self.assertEqual(payload["torrent_in"]["pubdate"], "2026-07-18")


class SubmissionGuardTests(IsolatedAppTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        settings.tr_base_url = ""
        settings.emby_base_url = ""
        settings.emby_api_key = ""

    def _seed_selection(self, candidate_id: str = "hardening-candidate") -> int:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("Hardening", 1, utc_now()),
            ).lastrowid)
            item_id = to_int(conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,original_title,year,chinese_title,library_state)
                   VALUES(?,?,?,?,?,?)""",
                (playlist_id, 1, "Hardening Movie", 2020, "测试电影", "not_found"),
            ).lastrowid)
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid)
            conn.execute(
                """INSERT INTO candidates(
                     id,task_id,playlist_item_id,candidate_index,title,site_name,resource_key,ranking,
                     eligibility,metadata_json,detail_url,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate_id, task_id, item_id, 0, "Hardening.Movie.1080p", "TestSite",
                    "hardening:1", 1, "eligible", "{}",
                    "https://tracker.example/details.php?id=42&passkey=database-secret&token=legacy-secret",
                    utc_now(),
                ),
            )
            conn.execute("INSERT INTO selection_items(candidate_id,selected_at) VALUES(?,?)", (candidate_id, utc_now()))
        raw_candidates[candidate_id] = {
            "media": {"id": 42, "title": "Hardening Movie", "type": "电影"},
            "torrent": {"title": "Hardening.Movie.1080p"},
        }
        return task_id

    async def test_candidate_and_selection_apis_clean_legacy_detail_url(self) -> None:
        task_id = self._seed_selection()
        candidates = task_candidates(task_id)
        self.assertEqual(candidates[0]["detail_url"], "https://tracker.example/details.php?id=42")
        self.assertEqual(candidates[0]["site_options"][0]["detail_url"], "https://tracker.example/details.php?id=42")
        items = await selection()
        self.assertEqual(items[0]["detail_url"], "https://tracker.example/details.php?id=42")

    async def test_transmission_failure_blocks_submission_and_keeps_selection(self) -> None:
        self._seed_selection("transmission-unknown")
        settings.tr_base_url = "http://transmission.example:9091"
        with patch.object(TransmissionClient, "current_downloads", new=AsyncMock(side_effect=RuntimeError("offline"))), \
             patch.object(MoviePilotClient, "download", new=AsyncMock()) as download:
            with self.assertRaises(HTTPException) as raised:
                await submit_selection()
        self.assertEqual(raised.exception.status_code, 503)
        download.assert_not_awaited()
        self.assertEqual([item["id"] for item in await selection()], ["transmission-unknown"])


class ActiveHistoryLookupTests(IsolatedAppTestCase):
    def test_active_history_matches_indexed_lookup(self):
        from app.services.history import _active_history_matches

        histories = [
            {"id": 1, "success": 1, "submission_hash": "aabbcc11", "torrent_name": "Movie.A.1080p"},
            {"id": 2, "success": 1, "submission_hash": "ddeeff22", "torrent_name": "Movie.B.1080p"},
            {"id": 3, "success": 1, "submission_hash": "ddeeff22", "torrent_name": "Movie.B.Dup"},
            {"id": 4, "success": 0, "submission_hash": "11223344", "torrent_name": "Movie.Failed"},
        ]
        torrents = [
            {"hashString": "aabbcc11", "name": "Movie.A.1080p", "status": 4},  # downloading
            {"hashString": "ddeeff22", "name": "Movie.B.1080p", "status": 4},  # ambiguous
            {"hashString": "11223344", "name": "Movie.Failed", "status": 4},   # not eligible
        ]
        matched, ambiguous = _active_history_matches(histories, torrents)
        self.assertIn(1, matched)
        self.assertNotIn(1, ambiguous)
        self.assertIn(2, ambiguous)
        self.assertIn(3, ambiguous)
        self.assertNotIn(4, matched)


class HistoryIdentityTests(IsolatedAppTestCase):
    def create_item(self, title="The Thing", year=1982, library_state="not_found"):
        with connect() as conn:
            playlist_id = conn.execute(
                "INSERT INTO playlists(name,created_at) VALUES('Audit','2026-09-13')",
            ).lastrowid
            item_id = conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,original_title,year,library_state)
                   VALUES(?,1,?,?,?)""", (playlist_id, title, year, library_state),
            ).lastrowid
        return playlist_id, item_id

    async def test_library_group_clear_preserves_failed_and_uses_orphan_snapshot(self):
        for state, group in (("in_library", "organized"), ("strm", "pending_library")):
            with self.subTest(group=group):
                _, item_id = self.create_item(library_state=state)
                with connect() as conn:
                    failed_id = conn.execute(
                        """INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at)
                           VALUES(?,'Failed','Failed',0,'2026-09-13')""", (item_id,),
                    ).lastrowid
                    conn.execute(
                        """INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at)
                           VALUES(?,'Success','Success',1,'2026-09-13')""", (item_id,),
                    )
                    conn.execute(
                        """INSERT INTO download_history(playlist_item_snapshot_json,title,torrent_name,success,created_at)
                           VALUES(?,'Orphan','Orphan',1,'2026-09-13')""", (json_value({"library_state": state}),),
                    )
                with patch("app.services.history.TransmissionClient.current_downloads", new=AsyncMock(return_value=[])):
                    deleted = await clear_download_history(group)
                self.assertEqual(deleted, 2)
                with connect() as conn:
                    self.assertIsNotNone(conn.execute("SELECT 1 FROM download_history WHERE id=?", (failed_id,)).fetchone())

    async def test_remake_does_not_block_queue_selection_or_match_history(self):
        playlist_id, item_id = self.create_item()
        with connect() as conn:
            item = dict(conn.execute("SELECT * FROM playlist_items WHERE id=?", (item_id,)).fetchone())
            conn.execute(
                """INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at)
                   VALUES(?,'The Thing','The.Thing.1982.1080p.Release',1,'2026-09-13')""", (item_id,),
            )
        torrent = {"name": "The.Thing.2011.1080p.Release", "status": 4, "percentDone": 0.2}
        candidate = {"title": "The.Thing.1982.1080p.Release"}
        self.assertFalse(_matches_active_torrent(candidate, item, [torrent]))
        with patch("app.services.search.TransmissionClient.current_downloads", new=AsyncMock(return_value=[torrent])):
            queue = await searchable_playlist_items(playlist_id)
            history = await projected_download_history()
        self.assertEqual([row["id"] for row in queue["items"]], [item_id])
        self.assertNotEqual(history[0]["lifecycle_status"], "downloading")
        self.assertTrue(_matches_active_torrent(candidate, item, [{**torrent, "name": "The.Thing.1982.720p.Other"}]))

    async def test_group_clear_reaches_matches_beyond_first_page(self):
        with connect() as conn:
            conn.execute("INSERT INTO download_history(title,torrent_name,success,created_at) VALUES('Old','Old',1,'2026-09-13')")
            conn.executemany(
                "INSERT INTO download_history(title,torrent_name,success,created_at) VALUES('Failed','Failed',0,'2026-09-13')",
                [() for _ in range(5000)],
            )
        with patch("app.services.history.TransmissionClient.current_downloads", new=AsyncMock(return_value=[])):
            self.assertEqual(await clear_download_history("submitted"), 1)
