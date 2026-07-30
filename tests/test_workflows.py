import json
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import main
from app.candidate_policy import DEFAULT_POLICY, merge_custom_rules, release_group_catalog
from app.clients import NexusPHPClient
from app.config import settings
from app.database import config_values, connect, initialize


class FunctionalWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous_data_dir = settings.data_dir
        settings.data_dir = self.temp.name
        main.raw_candidates.clear()
        initialize()

    async def asyncTearDown(self) -> None:
        main.raw_candidates.clear()
        settings.data_dir = self.previous_data_dir
        self.temp.cleanup()

    def create_playlist_item(self, title: str = "Workflow Movie", year: int = 2020) -> tuple[int, int]:
        with connect() as conn:
            playlist_id = int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("工作流测试", 1, main.utc_now()),
            ).lastrowid)
            item_id = int(conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title)
                   VALUES(?,?,?,?,?,?)""",
                (playlist_id, 1, "tt1234567", title, year, "工作流电影"),
            ).lastrowid)
        return playlist_id, item_id

    async def test_import_normalization_deduplicates_and_rebuilds_rank(self) -> None:
        payload = main.ImportPayload(json_data={"name": "混合片单", "films": [
            {"rank_no": 9, "imdb_id": "tt0000001", "original_title": "First", "year": 2001},
            {"rank_no": 20, "imdb_id": "tt0000001", "original_title": "Duplicate", "year": 2001},
            {"rank_no": 30, "tmdb_id": 222, "original_title": "Second", "year": "2002"},
            {"rank_no": 40, "original_title": "Same Title", "year": 2003},
            {"rank_no": 50, "original_title": "Same Title", "year": 2003},
            {"rank_no": 60, "original_title": "Same Title", "year": 2004},
        ]})
        name, items, source = await main.resolve_import(payload)
        self.assertEqual(name, "混合片单")
        self.assertEqual(source["source_type"], "json")
        self.assertEqual([item["rank_no"] for item in items], [1, 2, 3, 4])
        self.assertEqual([item["original_title"] for item in items], ["First", "Second", "Same Title", "Same Title"])

        preview = await main.preview_playlist_import(payload)
        imported = await main.import_playlist(payload)
        self.assertEqual(preview["count"], imported["count"])
        stored = await main.playlist_items(imported["id"])
        self.assertEqual([item["rank_no"] for item in stored], [1, 2, 3, 4])

    async def test_recognition_prefers_imdb_and_uses_ai_only_after_tmdb_miss(self) -> None:
        imdb_match = {"id": 101, "title": "Exact", "original_title": "Exact", "release_date": "2001-01-01"}
        tmdb = AsyncMock()
        tmdb.find_by_imdb.return_value = [imdb_match]
        with patch("app.services.recognition.TMDBClient", return_value=tmdb), patch("app.services.recognition.AIRecognitionClient") as ai_type:
            result = await main.recognize_movie("Exact", 2001, "tt0000001")
        self.assertEqual(result["id"], 101)
        tmdb.search_movie.assert_not_awaited()
        ai_type.return_value.suggest.assert_not_called()

        tmdb = AsyncMock()
        tmdb.find_by_imdb.return_value = []
        tmdb.search_movie.side_effect = [[], [{"id": 202, "title": "Corrected", "original_title": "Corrected", "release_date": "2002-02-02"}]]
        ai = AsyncMock()
        ai.suggest.return_value = {"original_title": "Corrected", "year": 2002}
        with patch("app.services.recognition.TMDBClient", return_value=tmdb), patch("app.services.recognition.AIRecognitionClient", return_value=ai):
            result = await main.recognize_movie("Wrong", 2002, "tt0000002")
        self.assertEqual(result["id"], 202)
        self.assertEqual(tmdb.search_movie.await_count, 2)
        ai.suggest.assert_awaited_once_with("Wrong", 2002)

    async def test_library_details_preserves_entity_strm_and_failure_states(self) -> None:
        emby = AsyncMock()
        emby.library_match.side_effect = [
            ("in_library", {"Id": "entity", "ImageTags": {"Primary": "p1"}}),
            ("strm", {"Id": "stream", "ImageTags": {"Primary": "p2"}}),
            RuntimeError("Emby unavailable"),
        ]
        self.assertEqual(await main.library_details(emby, "A", 2000), ("in_library", "entity", "p1"))
        self.assertEqual(await main.library_details(emby, "B", 2001), ("strm", "stream", "p2"))
        self.assertEqual(await main.library_details(emby, "C", 2002), ("unknown", None, None))

    async def test_candidate_policy_enforces_allowed_profiles_and_hard_exclusions(self) -> None:
        config = config_values()
        expected = {
            "Movie.2020.2160p.BluRay.x265-FRDS": "preferred",
            "Movie.2020.1080p.BluRay.x265-ADE": "preferred",
            "Movie.2020.1080p.BluRay.x265-HDSky": "preferred",
            "Movie.2020.1080p.BluRay.x265-CHD": "preferred",
            "Movie.2020.1080p.BluRay.x264-CMCT": "fallback",
            "Movie.2020.1080p.BluRay.x264-ADE": "excluded",
            "Movie.2020.1080p.BluRay.AVC.LPCM-DIY@HDHome": "excluded",
            "Movie.2020.2160p.REMUX.HEVC-FRDS": "excluded",
            "Movie.2020.2160p.WEB-DL.H265-HDSWEB": "excluded",
            "Movie.2020.BDMV.COMPLETE.BLURAY": "excluded",
        }
        analyzed = {title: main.analyze_candidate(title, index, config, {"seeders": 10, "volume_factor": 1}) for index, title in enumerate(expected)}
        self.assertEqual({title: item["recommendation"] for title, item in analyzed.items()}, expected)
        self.assertLess(analyzed["Movie.2020.2160p.BluRay.x265-FRDS"]["ranking"], analyzed["Movie.2020.1080p.BluRay.x264-CMCT"]["ranking"])
        self.assertIsNone(analyzed["Movie.2020.2160p.BluRay.x265-FRDS"]["exclusion_reason"])
        self.assertIn("DIY", analyzed["Movie.2020.1080p.BluRay.AVC.LPCM-DIY@HDHome"]["exclusion_reason"])

    async def test_tmdb_fields_are_canonical_and_search_plan_is_bounded(self) -> None:
        _, item_id = self.create_playlist_item("Imported Name", 1999)
        media = {"id": 88, "title": "TMDB 中文名", "original_title": "Canonical Original", "release_date": "2001-02-03", "imdb_id": "tt7654321"}
        main.persist_tmdb_item(item_id, media)
        with connect() as conn:
            item = conn.execute("SELECT * FROM playlist_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual(main.canonical_item_title(item), "TMDB 中文名")
        self.assertEqual(main.canonical_item_original_title(item), "Canonical Original")
        self.assertEqual(main.canonical_item_year(item), 2001)

    async def test_candidate_identity_rejects_wrong_year_and_collection_titles(self) -> None:
        item = {
            "tmdb_title": "安纳托利亚往事", "tmdb_original_title": "Once Upon a Time in Anatolia",
            "original_title": "Bir Zamanlar Anadolu'da", "chinese_title": "小亚细亚", "year": 2011,
            "tmdb_year": 2011,
        }
        media = {"year": "2011"}
        self.assertEqual(
            main.candidate_identity(item, media, "Bir Zamanlar Anadolu'da 2011 1080p BluRay"),
            (True, None),
        )
        accepted, reason = main.candidate_identity(item, media, "Once Upon a Time in China Trilogy 1991-1993")
        self.assertFalse(accepted)
        self.assertIn("年份不匹配", reason or "")
        accepted, reason = main.candidate_identity(item, media, "Once Upon a Time in China 2011 1080p BluRay")
        self.assertFalse(accepted)
        self.assertIn("片名不匹配", reason or "")
        accepted, reason = main.candidate_identity(item, media, "Bir Zamanlar Anadolu'da 2011 1080p BluRay")
        self.assertTrue(accepted)
        query_media = {**media, "year": "2001", "original_title": "Canonical Original", "imdb_id": "tt7654321"}
        queries = main.build_search_queries(item, query_media)
        self.assertEqual(queries[0][1], "tt7654321")
        self.assertLessEqual(len(queries), 4)
        self.assertTrue(any("Canonical Original 2001" in query[0] for query in queries))

    async def test_search_errors_have_actionable_chinese_summaries(self) -> None:
        code, message = main.classify_search_error(RuntimeError("[Errno -2] Name or service not known"))
        self.assertEqual(code, "dns_error")
        self.assertIn("无法解析站点地址", message)

        code, message = main.classify_search_error(RuntimeError("read timed out"))
        self.assertEqual(code, "timeout")
        self.assertIn("超时", message)

    async def test_release_group_catalog_merges_and_deduplicates_custom_rules(self) -> None:
        custom = merge_custom_rules(["MyStudio", "mystudio", "Studio-[A-Z]+"])
        self.assertEqual(custom, ["MyStudio", "Studio-[A-Z]+"])
        catalog = release_group_catalog({**DEFAULT_POLICY, "custom_release_groups": custom})
        self.assertGreater(catalog["builtin_count"], 60)
        self.assertEqual(catalog["custom_count"], 2)
        with self.assertRaises(main.HTTPException) as raised:
            await main.put_config(main.ConfigPayload(candidate_policy={**DEFAULT_POLICY, "custom_release_groups": ["["]}))
        self.assertEqual(raised.exception.status_code, 422)

    async def test_mocked_search_aggregates_sites_and_submits_through_moviepilot(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        with connect() as conn:
            for name, priority in (("Alpha", 1), ("Beta", 2), ("Broken", 3)):
                conn.execute(
                    """INSERT INTO pt_sites(name,adapter,base_url,priority,enabled,search_enabled,created_at)
                       VALUES(?,?,?,?,1,1,?)""",
                    (name, "nexusphp", f"https://{name.lower()}.example", priority, main.utc_now()),
                )
            task_id = int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, "queued", 1, main.utc_now(), main.utc_now()),
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

        with patch("app.services.search.recognize_movie", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.NexusPHPClient.search", new=search):
            await main.run_search(task_id)

        task = await main.task_status(task_id)
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["completed"], 1)
        self.assertEqual(task["matched"], 1)
        grouped = await main.candidates(task_id)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["site_count"], 2)
        self.assertEqual(grouped[0]["site_name"], "Alpha")
        self.assertEqual([option["site_name"] for option in grouped[0]["site_options"]], ["Alpha", "Beta"])
        logs = await main.task_logs(task_id)
        self.assertTrue(any(log["level"] == "warning" and "Broken" in log["message"] for log in logs))
        self.assertFalse(any("should-not-leak" in log["message"] for log in logs))
        attempts = await main.task_attempts(task_id)
        self.assertEqual(len(attempts["items"]), 3)
        self.assertEqual(sum(int(site["failed"] or 0) for site in attempts["sites"]), 1)
        retry_id, retry_total = main.create_followup_search_task(task_id, True)
        self.assertEqual(retry_total, 1)
        with connect() as conn:
            retry = conn.execute("SELECT parent_task_id,trigger,site_ids_json,item_ids_json FROM search_tasks WHERE id=?", (retry_id,)).fetchone()
        self.assertEqual(retry["parent_task_id"], task_id)
        self.assertEqual(retry["trigger"], "retry")
        self.assertEqual(len(json.loads(retry["site_ids_json"])), 1)
        self.assertEqual(json.loads(retry["item_ids_json"]), [item_id])

        restart_id, restart_total = main.create_followup_search_task(task_id, False)
        self.assertEqual(restart_total, 1)
        with connect() as conn:
            restart = conn.execute("SELECT parent_task_id,trigger,item_ids_json,site_ids_json FROM search_tasks WHERE id=?", (restart_id,)).fetchone()
        self.assertEqual(restart["parent_task_id"], task_id)
        self.assertEqual(restart["trigger"], "restart")
        self.assertEqual(restart["item_ids_json"], None)
        self.assertEqual(restart["site_ids_json"], None)

        candidate_id = grouped[0]["id"]
        toggled = await main.toggle_cart(candidate_id)
        self.assertTrue(toggled["in_cart"])
        submitted: list[tuple[dict, dict, str | None]] = []

        async def download(_client: object, media_in: dict, torrent_in: dict, downloader: str | None = None) -> dict:
            submitted.append((media_in, torrent_in, downloader))
            return {"success": True, "hash": "safe-hash"}

        with patch.object(main.MoviePilotClient, "download", new=download):
            result = await main.download_cart()
        self.assertEqual(result["mode"], "moviepilot")
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(submitted[0][2], "Transmission")
        self.assertEqual(submitted[0][0]["type"], "电影")
        self.assertEqual(submitted[0][0]["source"], "themoviedb")
        self.assertIsInstance(submitted[0][0]["year"], str)
        self.assertIn("passkey=super-secret", submitted[0][1]["enclosure"])
        self.assertEqual(await main.cart(), [])
        history = await main.history()
        self.assertTrue(history[0]["success"])
        self.assertNotIn("super-secret", json.dumps(history, ensure_ascii=False))

    async def test_retry_runs_only_the_exact_failed_item_site_pairs(self) -> None:
        playlist_id, first_item_id = self.create_playlist_item("First Movie", 2020)
        with connect() as conn:
            second_item_id = int(conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title)
                   VALUES(?,?,?,?,?,?)""",
                (playlist_id, 2, "tt7654321", "Second Movie", 2021, "第二部"),
            ).lastrowid)
            site_ids = []
            for name in ("Alpha", "Beta"):
                site_ids.append(int(conn.execute(
                    """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at)
                       VALUES(?,?,?,1,1,?)""",
                    (name, "nexusphp", f"https://{name.lower()}.example", main.utc_now()),
                ).lastrowid))
            source_id = int(conn.execute(
                """INSERT INTO search_tasks(
                     playlist_id,range_start,range_end,status,total,site_ids_json,item_ids_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (playlist_id, 1, 2, "partial", 2, json.dumps(site_ids), json.dumps([first_item_id, second_item_id]),
                 main.utc_now(), main.utc_now()),
            ).lastrowid)
            now = main.utc_now()
            conn.executemany(
                """INSERT INTO search_attempts(
                     task_id,playlist_item_id,site_id,site_name,status,result_count,duration_ms,created_at,finished_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                [
                    (source_id, first_item_id, site_ids[0], "Alpha", "failed", 0, 1, now, now),
                    (source_id, second_item_id, site_ids[1], "Beta", "failed", 0, 1, now, now),
                ],
            )
        retry_id, _ = main.create_followup_search_task(source_id, True)
        with connect() as conn:
            retry = conn.execute("SELECT pair_scope_json FROM search_tasks WHERE id=?", (retry_id,)).fetchone()
        self.assertEqual(
            {tuple(pair) for pair in json.loads(retry["pair_scope_json"])},
            {(first_item_id, site_ids[0]), (second_item_id, site_ids[1])},
        )
        seen: list[tuple[int, int]] = []

        async def fake_search_site(_task_id: int, item: object, site: dict, *_args: object) -> tuple[dict, list, None, int]:
            seen.append((int(item["id"]), int(site["id"])))  # type: ignore[index]
            return site, [], None, 1

        media = {"id": 91, "title": "Movie", "original_title": "Movie", "release_date": "2020-01-01"}
        with patch("app.services.search.recognize_movie", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.search_one_site", new=fake_search_site):
            await main.run_search(retry_id)
        self.assertEqual(set(seen), {(first_item_id, site_ids[0]), (second_item_id, site_ids[1])})
        self.assertEqual(len(seen), 2)

    async def test_moviepilot_false_response_is_recorded_as_failure(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        with connect() as conn:
            task_id = int(conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid)
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,site_name,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("false-response", task_id, item_id, 0, "Workflow.Movie.1080p", "Test", 1, "{}", main.utc_now()),
            )
            conn.execute("INSERT INTO cart_items(candidate_id,selected_at) VALUES(?,?)", ("false-response", main.utc_now()))
        main.raw_candidates["false-response"] = {
            "media": {"id": 1, "title": "Workflow Movie", "type": "电影"},
            "torrent": {"title": "Workflow.Movie.1080p"},
        }
        with patch.object(main.MoviePilotClient, "download", new=AsyncMock(return_value={"success": "false", "message": "下游拒绝"})):
            result = await main.download_cart()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual((await main.cart())[0]["id"], "false-response")
        with connect() as conn:
            history = conn.execute("SELECT success,message FROM download_history WHERE candidate_id=?", ("false-response",)).fetchone()
        self.assertEqual(history["success"], 0)
        self.assertIn("下游拒绝", history["message"])

    async def test_search_snapshot_missing_item_fails_explicitly(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        with connect() as conn:
            site_id = int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,created_at) VALUES(?,?,?,?)",
                ("Snapshot Site", "rss", "https://snapshot.example/feed", main.utc_now()),
            ).lastrowid)
            task_id = int(conn.execute(
                """INSERT INTO search_tasks(
                     playlist_id,range_start,range_end,status,total,site_ids_json,item_ids_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, "queued", 1, json.dumps([site_id]), json.dumps([item_id]), main.utc_now(), main.utc_now()),
            ).lastrowid)
            conn.execute("DELETE FROM playlist_items WHERE id=?", (item_id,))
        await main.run_search(task_id)
        task = await main.task_status(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("影片已不存在", task["error_message"])

    async def test_followup_rejects_running_source_task(self) -> None:
        playlist_id, _item_id = self.create_playlist_item()
        with connect() as conn:
            task_id = int(conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (playlist_id, 1, 1, "running", 1, main.utc_now(), main.utc_now()),
            ).lastrowid)
        with self.assertRaises(main.HTTPException) as raised:
            main.create_followup_search_task(task_id, True)
        self.assertEqual(raised.exception.status_code, 409)

    async def test_restart_copies_original_item_and_site_snapshots(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        with connect() as conn:
            site_id = int(conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at)
                   VALUES(?,?,?,1,1,?)""",
                ("Original", "rss", "https://original.example/feed", main.utc_now()),
            ).lastrowid)
            source_id = int(conn.execute(
                """INSERT INTO search_tasks(
                     playlist_id,range_start,range_end,status,total,site_ids_json,item_ids_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, "completed", 1, json.dumps([site_id]), json.dumps([item_id]),
                 main.utc_now(), main.utc_now()),
            ).lastrowid)
            conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at)
                   VALUES(?,?,?,1,1,?)""",
                ("New Site", "rss", "https://new.example/feed", main.utc_now()),
            )
            conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,original_title,year)
                   VALUES(?,?,?,?)""",
                (playlist_id, 2, "New Movie", 2024),
            )
        restart_id, restart_total = main.create_followup_search_task(source_id, False)
        with connect() as conn:
            restart = conn.execute(
                "SELECT site_ids_json,item_ids_json FROM search_tasks WHERE id=?", (restart_id,),
            ).fetchone()
        self.assertEqual(restart_total, 1)
        self.assertEqual(json.loads(restart["site_ids_json"]), [site_id])
        self.assertEqual(json.loads(restart["item_ids_json"]), [item_id])

    async def test_excluded_candidate_cannot_enter_cart(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        candidate_id = "excluded-candidate"
        with connect() as conn:
            task_id = conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (playlist_id, 1, 1, "completed", 1, main.utc_now(), main.utc_now()),
            ).lastrowid
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,eligibility,exclusion_reason,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, task_id, item_id, 0, "Movie.REMUX", "excluded", "包含 REMUX", 1, "{}", main.utc_now()),
            )
        main.raw_candidates[candidate_id] = {"media": {}, "torrent": {}}
        with self.assertRaises(main.HTTPException) as raised:
            await main.toggle_cart(candidate_id)
        self.assertEqual(raised.exception.status_code, 422)

    async def test_adapter_selection_and_volume_factor_contracts(self) -> None:
        self.assertEqual(main.resolve_site_adapter("https://kp.m-team.cc"), "mteam")
        self.assertEqual(main.resolve_site_adapter("https://indexer.test/api?t=caps"), "torznab")
        self.assertEqual(main.resolve_site_adapter("https://nexus.example"), "nexusphp")
        self.assertEqual(main.resolve_site_adapter("https://nexus.example", "https://nexus.example/rss?key=private"), "rss")
        self.assertEqual(main.volume_factor_value("FREE"), 0)
        self.assertEqual(main.volume_factor_value("50%"), 0.5)
        self.assertEqual(main.volume_factor_value("normal"), 1)

    async def test_nexusphp_decodes_title_and_extracts_binary_size(self) -> None:
        response = Mock()
        response.text = """<table><tr>
          <td>电影</td><td><table><tr><td>预览</td><td>
            <a href="details.php?id=42">&nbsp;Movie.2020.1080p.x265-FRDS</a>
          </td></tr></table></td>
          <td><a href="download.php?id=42&amp;passkey=dummy">下载</a></td>
          <td>8.25<br>GiB</td><td>12</td><td>3</td><td>99</td>
        </tr></table>"""
        response.raise_for_status = Mock()
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch("app.clients.httpx.AsyncClient", return_value=context):
            rows = await NexusPHPClient().search(
                {"name": "测试站", "base_url": "https://tracker.example", "cookie": "dummy"},
                "Movie",
                "tt0000042",
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Movie.2020.1080p.x265-FRDS")
        self.assertEqual(rows[0]["size"], int(8.25 * 1024**3))
        self.assertEqual(rows[0]["seeders"], 12)

    async def test_nexusphp_account_stats_parse_combined_profile_cell(self) -> None:
        home = Mock()
        home.text = '<a href="userdetails.php?id=42">账户</a>'
        home.raise_for_status = Mock()
        details = Mock()
        details.text = """<table><tr><td>
          上传量：12.5 TiB<br>下载量：2.5 TiB<br>分享率：5.00<br>魔力值：1234.5<br>做种数：86
        </td></tr></table>"""
        details.raise_for_status = Mock()
        client = AsyncMock()
        client.get.side_effect = [home, details]
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch("app.clients.httpx.AsyncClient", return_value=context):
            stats = await NexusPHPClient().account_stats({
                "name": "测试站",
                "base_url": "https://tracker.example",
                "cookie": "session=dummy",
            })
        self.assertEqual(stats["uploaded"], int(12.5 * 1024**4))
        self.assertEqual(stats["downloaded"], int(2.5 * 1024**4))
        self.assertEqual(stats["ratio"], 5.0)
        self.assertEqual(stats["seeding"], 86)

    async def test_moviepilot_payload_maps_internal_torrent_fields(self) -> None:
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = {"success": True}
        client = AsyncMock()
        client.post.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch.object(main.MoviePilotClient, "_client", return_value=context):
            await main.MoviePilotClient().download(
                {"year": "1982"},
                {"title": "Movie", "volume_factor": 0.5, "publish_time": "2026-07-18"},
                downloader="Transmission",
            )
        payload = client.post.await_args.kwargs["json"]
        self.assertNotIn("volume_factor", payload["torrent_in"])
        self.assertNotIn("publish_time", payload["torrent_in"])
        self.assertEqual(payload["torrent_in"]["downloadvolumefactor"], 0.5)
        self.assertEqual(payload["torrent_in"]["pubdate"], "2026-07-18")
