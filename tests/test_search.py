"""寻片：检索计划、候选规则与去重、寻片队列、站点限流与重试。"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.search import create_task, task_status
from app.api.selection import selection, submit_selection
from app.api.system import put_config
from app.candidate_policy import DEFAULT_POLICY, merge_custom_rules, release_group_catalog
from app.clients import MoviePilotClient, TransmissionClient
from app.config import settings
from app.database import config_values, connect
from app.domain.titles import (
    candidate_identity,
    canonical_item_original_title,
    canonical_item_title,
    canonical_item_year,
)
from app.main import app
from app.schemas import ConfigPayload, TaskPayload
from app.sites.errors import SearchCaptcha
from app.services.recognition import analyze_candidate, persist_tmdb_item
from app.services.search import (
    build_search_queries,
    classify_search_error,
    create_followup_search_task,
    run_search,
    searchable_playlist_items,
)
from app.state import raw_candidates
from app.tasks import SEARCH
from app.util import to_int, utc_now
from tests.support import IsolatedAppTestCase, SeededPlaylistTestCase, task_candidates


class CandidateAndQueueTests(SeededPlaylistTestCase):
    async def test_candidates_tolerate_corrupted_legacy_metadata(self) -> None:
        with connect() as conn:
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO candidates(id,task_id,playlist_item_id,candidate_index,title,group_tier,ranking,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("damaged", task_id, item_id, 0, "Example 1080p", 9, 1, "not-json", utc_now()),
            )
        result = task_candidates(to_int(task_id))
        self.assertEqual(result[0]["metadata"], {})

    async def test_searchable_queue_excludes_transmission_downloads_and_limits_front(self) -> None:
        with patch.object(
            TransmissionClient, "current_downloads", new=AsyncMock(return_value=[
                {"name": "Movie 2 2002 1080p", "status": 4, "percentDone": 0.25},
            ]),
        ):
            queue = await searchable_playlist_items(self.playlist_id, limit=50)
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
                ("测试站点", "rss", "https://example.com/feed", utc_now()),
            )
        with patch.object(TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])), \
             patch("app.api.search.run_search", new=AsyncMock()):
            result = await create_task(TaskPayload(playlist_id=self.playlist_id, scope="pending", count=50))
            await SEARCH.running[result["id"]]
        with connect() as conn:
            task = conn.execute("SELECT * FROM search_tasks WHERE id=?", (result["id"],)).fetchone()
        selected = json.loads(task["item_ids_json"])
        self.assertEqual(result["total"], 50)
        self.assertEqual(len(selected), 50)
        self.assertEqual(task["trigger"], "pending")
        self.assertTrue(json.loads(task["site_ids_json"]))

    async def test_candidate_identity_rejects_sequel_and_plural_variants(self) -> None:
        item = {
            "tmdb_title": "教父", "tmdb_original_title": "The Godfather",
            "original_title": "The Godfather", "chinese_title": "教父", "year": 1972, "tmdb_year": 1972,
        }
        media = {"year": "1972"}
        accepted, reason = candidate_identity(item, media, "The.Godfather.Part.II.1080p")
        self.assertFalse(accepted)
        self.assertIn("续集或分卷", reason or "")
        accepted, reason = candidate_identity(item, media, "The.Godfathers.1972.1080p")
        self.assertFalse(accepted)
        self.assertIn("片名不匹配", reason or "")
        # 正式片名本身含 Part II 时不应被自己的种子标题拦截。
        item_sequel = {**item, "tmdb_original_title": "The Godfather Part II", "original_title": "The Godfather Part II", "tmdb_title": "教父2", "chinese_title": "教父2", "year": 1974, "tmdb_year": 1974}
        accepted, _ = candidate_identity(item_sequel, {"year": "1974"}, "The.Godfather.Part.II.1974.1080p")
        self.assertTrue(accepted)
        # 年份匹配的正片仍然通过。
        accepted, _ = candidate_identity(item, media, "The.Godfather.1972.1080p.BluRay")
        self.assertTrue(accepted)

    async def test_avc_bdrip_not_rejected_as_raw_disc(self) -> None:
        """AVC 编码的 BDrip 不再被误判为完整原盘。"""
        config = config_values()
        analyzed = analyze_candidate("Movie.2024.1080p.BluRay.AVC.LPCM-BDRIP", 0, config, {"seeders": 5, "volume_factor": 1})
        self.assertNotIn("原盘", analyzed.get("exclusion_reason") or "")
        still_rejected = analyze_candidate("Movie.2024.1080p.BluRay.AVC.DTS-HD.MA", 0, config, {"seeders": 5, "volume_factor": 1})
        self.assertIn("原盘", still_rejected.get("exclusion_reason") or "")

    async def test_nested_quantifier_release_group_rule_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await put_config(ConfigPayload(candidate_policy={
                **DEFAULT_POLICY, "custom_release_groups": ["(?:A+)+B"],
            }))
        self.assertEqual(raised.exception.status_code, 422)
        # 普通组后量词（无嵌套）仍然合法。
        catalog = release_group_catalog({**DEFAULT_POLICY, "custom_release_groups": ["(?:AB|CD)+"]})
        self.assertEqual(catalog["custom_count"], 1)

    async def test_searchable_queue_keeps_history_without_candidate(self) -> None:
        """候选被删除的历史提交仍能排除下载中的影片。"""
        with connect() as conn:
            item = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no=2", (self.playlist_id,),
            ).fetchone()
            conn.execute(
                """INSERT INTO download_history(playlist_item_id,title,torrent_name,success,created_at)
                   VALUES(?,?,?,1,?)""",
                (item["id"], "Movie 2", "Movie 2 2002 1080p", utc_now()),
            )
        with patch.object(TransmissionClient, "current_downloads", new=AsyncMock(return_value=[
            {"name": "Movie 2 2002 1080p", "status": 4, "percentDone": 0.1},
        ])):
            queue = await searchable_playlist_items(self.playlist_id, limit=10)
        self.assertNotIn(2, [item["rank_no"] for item in queue["items"]])

    async def test_zero_seeder_candidate_excluded(self) -> None:
        """0 人做种的资源无法下载，直接排除；有做种者的同标题资源不受影响。"""
        config = config_values()
        excluded = analyze_candidate(
            "Movie.2024.1080p.x265-FRDS", 0, config, {"seeders": 0, "volume_factor": 1},
        )
        self.assertFalse(excluded["eligible"])
        self.assertIn("0 人做种", excluded.get("exclusion_reason") or "")
        eligible = analyze_candidate(
            "Movie.2024.1080p.x265-FRDS", 0, config, {"seeders": 3, "volume_factor": 1},
        )
        self.assertTrue(eligible["eligible"])

    async def test_spoofed_release_group_codec_rejected(self) -> None:
        """Fury（x265 组）与 SPM（x264 组）编码不符时判定为冒用组名；@站点名 不再误识别为 HDS 组。"""
        config = config_values()
        spoofed = analyze_candidate(
            "Movie.2024.1080p.BluRay.x264-Fury@HDSky", 0, config, {"seeders": 5, "volume_factor": 1},
        )
        self.assertFalse(spoofed["eligible"])
        self.assertIn("FURY 组为 x265", spoofed.get("exclusion_reason") or "")
        spoofed = analyze_candidate(
            "Movie.2024.2160p.x265-SPM@HDSky", 0, config, {"seeders": 5, "volume_factor": 1},
        )
        self.assertFalse(spoofed["eligible"])
        self.assertIn("SPM 组为 x264", spoofed.get("exclusion_reason") or "")
        # 编码与知名组惯例一致时不再误报冒用；组名识别为 FURY（而非 HDSky 的 HDS）。
        genuine = analyze_candidate(
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
                (self.playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,priority,created_at) VALUES(?,?,?,?,?)",
                ("聚合测试站A", "nexusphp", "https://site-a.example", 1, utc_now()),
            )
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,priority,created_at) VALUES(?,?,?,?,?)",
                ("聚合测试站B", "nexusphp", "https://site-b.example", 5, utc_now()),
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
                     "unknown", 0, "eligible", None, "primary_x265", "{}", utc_now()),
                )
        result = task_candidates(to_int(task_id))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["site_name"], "聚合测试站B")
        self.assertEqual(result[0]["seeders"], 80)
        self.assertEqual(result[0]["site_count"], 2)
        self.assertEqual(len(result[0]["site_options"]), 2)
        options_by_site = {option["site_name"]: option for option in result[0]["site_options"]}
        self.assertEqual(options_by_site["聚合测试站A"]["seeders"], 2)
        self.assertEqual(options_by_site["聚合测试站B"]["seeders"], 80)


class SearchWorkflowTests(IsolatedAppTestCase):
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
        analyzed = {title: analyze_candidate(title, index, config, {"seeders": 10, "volume_factor": 1}) for index, title in enumerate(expected)}
        self.assertEqual({title: item["recommendation"] for title, item in analyzed.items()}, expected)
        self.assertLess(analyzed["Movie.2020.2160p.BluRay.x265-FRDS"]["ranking"], analyzed["Movie.2020.1080p.BluRay.x264-CMCT"]["ranking"])
        self.assertIsNone(analyzed["Movie.2020.2160p.BluRay.x265-FRDS"]["exclusion_reason"])
        self.assertIn("DIY", analyzed["Movie.2020.1080p.BluRay.AVC.LPCM-DIY@HDHome"]["exclusion_reason"])

    async def test_tmdb_fields_are_canonical_and_search_plan_is_bounded(self) -> None:
        _, item_id = self.create_playlist_item("Imported Name", 1999)
        media = {"id": 88, "title": "TMDB 中文名", "original_title": "Canonical Original", "release_date": "2001-02-03", "imdb_id": "tt7654321"}
        persist_tmdb_item(item_id, media)
        with connect() as conn:
            item = conn.execute("SELECT * FROM playlist_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual(canonical_item_title(item), "TMDB 中文名")
        self.assertEqual(canonical_item_original_title(item), "Canonical Original")
        self.assertEqual(canonical_item_year(item), 2001)

    async def test_candidate_identity_rejects_wrong_year_and_collection_titles(self) -> None:
        item = {
            "tmdb_title": "安纳托利亚往事", "tmdb_original_title": "Once Upon a Time in Anatolia",
            "original_title": "Bir Zamanlar Anadolu'da", "chinese_title": "小亚细亚", "year": 2011,
            "tmdb_year": 2011,
        }
        media = {"year": "2011"}
        self.assertEqual(
            candidate_identity(item, media, "Bir Zamanlar Anadolu'da 2011 1080p BluRay"),
            (True, None),
        )
        accepted, reason = candidate_identity(item, media, "Once Upon a Time in China Trilogy 1991-1993")
        self.assertFalse(accepted)
        self.assertIn("年份不匹配", reason or "")
        accepted, reason = candidate_identity(item, media, "Once Upon a Time in China 2011 1080p BluRay")
        self.assertFalse(accepted)
        self.assertIn("片名不匹配", reason or "")
        accepted, reason = candidate_identity(item, media, "Bir Zamanlar Anadolu'da 2011 1080p BluRay")
        self.assertTrue(accepted)
        query_media = {**media, "year": "2001", "original_title": "Canonical Original", "imdb_id": "tt7654321"}
        queries = build_search_queries(dict(item), query_media)
        self.assertEqual(queries[0][1], "tt7654321")
        self.assertLessEqual(len(queries), 4)
        # 检索词优先用片单来源年份（种子多按首映年份命名），TMDB 年份只在来源缺年份时使用。
        self.assertTrue(any("Canonical Original 2011" in query[0] for query in queries))
        no_year = build_search_queries({**item, "year": None}, query_media)
        self.assertTrue(any("Canonical Original 2001" in query[0] for query in no_year))

    async def test_candidate_identity_allows_one_year_difference(self) -> None:
        # 卡萨布兰卡：片单 1942，TMDB 1943；种子标题写 1942，不能因此排除。
        item = {
            "tmdb_title": "卡萨布兰卡", "tmdb_original_title": "Casablanca", "original_title": "Casablanca",
            "chinese_title": "卡萨布兰卡", "year": 1942, "tmdb_year": 1943,
        }
        media = {"year": "1943"}
        self.assertEqual(candidate_identity(item, media, "Casablanca 1942 2160p UHD BluRay x265-FRDS"), (True, None))
        self.assertEqual(candidate_identity(item, {}, "Casablanca 1944 1080p BluRay x265-CHD"), (True, None))
        accepted, reason = candidate_identity(item, media, "Casablanca 2012 1080p BluRay x265-CHD")
        self.assertFalse(accepted)
        self.assertEqual(reason, "年份不匹配：目标 1942 / 1943，资源包含 2012")
        self.assertIn("Casablanca 1942", [query[0] for query in build_search_queries(item, media)])

    async def test_search_errors_have_actionable_chinese_summaries(self) -> None:
        code, message = classify_search_error(RuntimeError("[Errno -2] Name or service not known"))
        self.assertEqual(code, "dns_error")
        self.assertIn("无法解析站点地址", message)

        code, message = classify_search_error(RuntimeError("read timed out"))
        self.assertEqual(code, "timeout")
        self.assertIn("超时", message)

        # 站点拦截直接沿用站点给出的说明，不再统一成“请查看日志”。
        code, message = classify_search_error(SearchCaptcha())
        self.assertEqual(code, "site_blocked")
        self.assertIn("人机验证", message)

    async def test_release_group_catalog_merges_and_deduplicates_custom_rules(self) -> None:
        custom = merge_custom_rules(["MyStudio", "mystudio", "Studio-[A-Z]+"])
        self.assertEqual(custom, ["MyStudio", "Studio-[A-Z]+"])
        catalog = release_group_catalog({**DEFAULT_POLICY, "custom_release_groups": custom})
        self.assertGreater(catalog["builtin_count"], 60)
        self.assertEqual(catalog["custom_count"], 2)
        with self.assertRaises(HTTPException) as raised:
            await put_config(ConfigPayload(candidate_policy={**DEFAULT_POLICY, "custom_release_groups": ["["]}))
        self.assertEqual(raised.exception.status_code, 422)

    async def test_retry_runs_only_the_exact_failed_item_site_pairs(self) -> None:
        playlist_id, first_item_id = self.create_playlist_item("First Movie", 2020)
        with connect() as conn:
            second_item_id = to_int(conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title)
                   VALUES(?,?,?,?,?,?)""",
                (playlist_id, 2, "tt7654321", "Second Movie", 2021, "第二部"),
            ).lastrowid)
            site_ids = []
            for name in ("Alpha", "Beta"):
                site_ids.append(to_int(conn.execute(
                    """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at)
                       VALUES(?,?,?,1,1,?)""",
                    (name, "nexusphp", f"https://{name.lower()}.example", utc_now()),
                ).lastrowid))
            source_id = to_int(conn.execute(
                """INSERT INTO search_tasks(
                     playlist_id,range_start,range_end,status,total,site_ids_json,item_ids_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (playlist_id, 1, 2, "partial", 2, json.dumps(site_ids), json.dumps([first_item_id, second_item_id]),
                 utc_now(), utc_now()),
            ).lastrowid)
            now = utc_now()
            conn.executemany(
                """INSERT INTO search_attempts(
                     task_id,playlist_item_id,site_id,site_name,status,result_count,duration_ms,created_at,finished_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                [
                    (source_id, first_item_id, site_ids[0], "Alpha", "failed", 0, 1, now, now),
                    (source_id, second_item_id, site_ids[1], "Beta", "failed", 0, 1, now, now),
                ],
            )
        retry_id, _ = create_followup_search_task(source_id, True)
        with connect() as conn:
            retry = conn.execute("SELECT pair_scope_json FROM search_tasks WHERE id=?", (retry_id,)).fetchone()
        self.assertEqual(
            {tuple(pair) for pair in json.loads(retry["pair_scope_json"])},
            {(first_item_id, site_ids[0]), (second_item_id, site_ids[1])},
        )
        seen: list[tuple[int, int]] = []

        async def fake_search_site(_task_id: int, item: object, site: dict, *_args: object, **_kwargs: object) -> tuple:
            seen.append((to_int(item["id"]), to_int(site["id"])))  # type: ignore[index]
            return site, [], None, 1, None

        media = {"id": 91, "title": "Movie", "original_title": "Movie", "release_date": "2020-01-01"}
        with patch("app.services.search.recognize_item", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.search_one_site", new=fake_search_site):
            await run_search(retry_id)
        self.assertEqual(set(seen), {(first_item_id, site_ids[0]), (second_item_id, site_ids[1])})
        self.assertEqual(len(seen), 2)

    async def test_search_snapshot_missing_item_fails_explicitly(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,created_at) VALUES(?,?,?,?)",
                ("Snapshot Site", "rss", "https://snapshot.example/feed", utc_now()),
            ).lastrowid)
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(
                     playlist_id,range_start,range_end,status,total,site_ids_json,item_ids_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, "queued", 1, json.dumps([site_id]), json.dumps([item_id]), utc_now(), utc_now()),
            ).lastrowid)
            conn.execute("DELETE FROM playlist_items WHERE id=?", (item_id,))
        await run_search(task_id)
        task = await task_status(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("影片已不存在", task["error_message"])

    async def test_followup_rejects_running_source_task(self) -> None:
        playlist_id, _item_id = self.create_playlist_item()
        with connect() as conn:
            task_id = to_int(conn.execute(
                "INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (playlist_id, 1, 1, "running", 1, utc_now(), utc_now()),
            ).lastrowid)
        with self.assertRaises(HTTPException) as raised:
            create_followup_search_task(task_id, True)
        self.assertEqual(raised.exception.status_code, 409)

    async def test_restart_keeps_items_but_uses_current_searchable_sites(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        with connect() as conn:
            site_id = to_int(conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at)
                   VALUES(?,?,?,1,1,?)""",
                ("Original", "rss", "https://original.example/feed", utc_now()),
            ).lastrowid)
            source_id = to_int(conn.execute(
                """INSERT INTO search_tasks(
                     playlist_id,range_start,range_end,status,total,site_ids_json,item_ids_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, "completed", 1, json.dumps([site_id]), json.dumps([item_id]),
                 utc_now(), utc_now()),
            ).lastrowid)
            # 原任务之后：原站点停用搜索，新增一个参与搜索的站点，片单也新增一部。
            conn.execute("UPDATE pt_sites SET search_enabled=0 WHERE id=?", (site_id,))
            new_site_id = to_int(conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at)
                   VALUES(?,?,?,1,1,?)""",
                ("New Site", "rss", "https://new.example/feed", utc_now()),
            ).lastrowid)
            conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,original_title,year)
                   VALUES(?,?,?,?)""",
                (playlist_id, 2, "New Movie", 2024),
            )
        restart_id, restart_total = create_followup_search_task(source_id, False)
        with connect() as conn:
            restart = conn.execute(
                "SELECT site_ids_json,item_ids_json FROM search_tasks WHERE id=?", (restart_id,),
            ).fetchone()
        self.assertEqual(restart_total, 1)
        self.assertIsNone(restart["site_ids_json"])
        self.assertEqual(json.loads(restart["item_ids_json"]), [item_id])
        seen: list[int] = []

        async def fake_search_site(_task_id: int, _item: object, site: dict, *_args: object, **_kwargs: object) -> tuple:
            seen.append(to_int(site["id"]))
            return site, [], None, 1, None

        media = {"id": 91, "title": "Movie", "original_title": "Movie", "release_date": "2020-01-01"}
        with patch("app.services.search.recognize_item", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.search_one_site", new=fake_search_site):
            await run_search(restart_id)
        self.assertEqual(seen, [new_site_id])

    async def test_retry_skips_sites_no_longer_searchable(self) -> None:
        playlist_id, item_id = self.create_playlist_item()
        with connect() as conn:
            kept, disabled = (to_int(conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at)
                   VALUES(?,?,?,1,1,?)""",
                (name, "rss", f"https://{name}.example/feed", utc_now()),
            ).lastrowid) for name in ("kept", "disabled"))
            source_id = to_int(conn.execute(
                """INSERT INTO search_tasks(
                     playlist_id,range_start,range_end,status,total,site_ids_json,item_ids_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (playlist_id, 1, 1, "partial", 1, json.dumps([kept, disabled]), json.dumps([item_id]), utc_now(), utc_now()),
            ).lastrowid)
            now = utc_now()
            for site_id in (kept, disabled):
                conn.execute(
                    """INSERT INTO search_attempts(task_id,playlist_item_id,site_id,site_name,status,result_count,duration_ms,created_at,finished_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (source_id, item_id, site_id, "site", "failed", 0, 1, now, now),
                )
            conn.execute("UPDATE pt_sites SET search_enabled=0 WHERE id=?", (disabled,))
        retry_id, _ = create_followup_search_task(source_id, True)
        with connect() as conn:
            retry = conn.execute("SELECT site_ids_json,pair_scope_json FROM search_tasks WHERE id=?", (retry_id,)).fetchone()
        self.assertEqual(json.loads(retry["site_ids_json"]), [kept])
        self.assertEqual(json.loads(retry["pair_scope_json"]), [[item_id, kept]])
        with connect() as conn:
            conn.execute("UPDATE pt_sites SET search_enabled=0 WHERE id=?", (kept,))
        with self.assertRaises(HTTPException) as raised:
            create_followup_search_task(source_id, True)
        self.assertEqual(raised.exception.status_code, 422)

    async def test_blocked_site_is_skipped_for_remaining_films(self) -> None:
        playlist_id, first_id = self.create_playlist_item()
        with connect() as conn:
            second_id = to_int(conn.execute(
                "INSERT INTO playlist_items(playlist_id,rank_no,original_title,year) VALUES(?,?,?,?)",
                (playlist_id, 2, "Second", 2021),
            ).lastrowid)
            blocked, healthy = (to_int(conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at)
                   VALUES(?,?,?,1,1,?)""",
                (name, "nexusphp", f"https://{name}.example", utc_now()),
            ).lastrowid) for name in ("captcha", "healthy"))
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,item_ids_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (playlist_id, 1, 2, "queued", 2, json.dumps([first_id, second_id]), utc_now(), utc_now()),
            ).lastrowid)
        calls: list[tuple[str, str]] = []

        class Client:
            async def search(self, site: dict, title: str, imdb_id: str | None) -> list[dict]:
                calls.append((site["name"], title))
                if site["name"] == "captcha":
                    raise SearchCaptcha()
                return []

        media = {"id": 91, "title": "Movie", "original_title": "Movie", "release_date": "2020-01-01"}
        with patch("app.services.search.recognize_item", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.NexusPHPClient", return_value=Client()), \
             patch("app.services.search.SEARCH_FILM_CONCURRENCY", 1):
            await run_search(task_id)
        # 逐部搜索时，被拦截的站点只请求一次（第一部片的第一个检索词），之后不再请求。
        self.assertEqual(sum(1 for name, _ in calls if name == "captcha"), 1)
        with connect() as conn:
            attempts = conn.execute(
                "SELECT playlist_item_id,status,error_code,error_message FROM search_attempts WHERE task_id=? AND site_id=? ORDER BY id",
                (task_id, blocked),
            ).fetchall()
            task = conn.execute("SELECT status,error_message FROM search_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual([(row["playlist_item_id"], row["status"], row["error_code"]) for row in attempts],
                         [(first_id, "failed", "site_blocked"), (second_id, "failed", "site_blocked")])
        self.assertIn("本次任务已跳过", attempts[1]["error_message"])
        self.assertEqual(task["status"], "partial")
        self.assertIn("人机验证", task["error_message"])
        retry_id, retry_total = create_followup_search_task(task_id, True)
        self.assertEqual(retry_total, 2)


class TitleIdentityRegressionTests(IsolatedAppTestCase):
    """任务 #2 里被误排除的真实标题。"""

    def test_single_film_editions_are_not_collections(self) -> None:
        from app.domain.titles import is_collection_title

        single = [
            "Double Indemnity 1944 2160p Criterion Collection UHD BluRay x265 10bit HDR DTS-HD MA 1.0-ADE",
            "Harakiri 1962 CC Repack 1080p BluRay x265 10bit FLAC-ADE",
            "Memories of Murder REPACK 2003 4K REMASTERED FRA BluRay 1080p x264 DTS 2Audios-CMCT",
            "Raise the Red Lantern 1991 BluRay 1080p x264 FLAC-CMCT 大红灯笼高高挂 / Épouses et concubines 张艺谋作品系列 内封简繁字幕",
            "Chungking Express 1994 JPN 1080p BluRay x264 DTS-2Audios-CMCT 重庆森林 国粤双语日本修复版 王家卫作品系列之（三）",
        ]
        collections = [
            "The Godfather Trilogy 1972-1990 BluRay 1080p x265 10bit MNHD-FRDS",
            "[教父I II III合集] The Godfather 1972-1990 BluRay 720p x264 AC3 2Audios-CMCT",
            "Kar-Wai Wong Film Collection 1988-2013 BluRay 1080p x265 10bit MNHD-FRDS",
            "Alien Quadrilogy Box Set 1979-1997 BluRay 1080p x264-CMCT",
            "教父三部曲 The Godfather BluRay 1080p x265-FRDS",
        ]
        for title in single:
            with self.subTest(title=title[:40]):
                self.assertFalse(is_collection_title(title))
        for title in collections:
            with self.subTest(title=title[:40]):
                self.assertTrue(is_collection_title(title))
        item = {
            "tmdb_title": "双重赔偿", "tmdb_original_title": "Double Indemnity", "original_title": "Double Indemnity",
            "chinese_title": None, "year": 1944, "tmdb_year": 1944,
        }
        self.assertEqual(candidate_identity(item, {"year": "1944"}, single[0]), (True, None))
        # 种子带的 IMDb 编号与目标一致时，CC 版不再被当成合集。
        item["tmdb_imdb_id"] = "tt0036775"
        self.assertEqual(candidate_identity(item, {"year": "1944", "imdb_id": "tt0036775"}, single[0], "tt0036775"), (True, None))

    def test_accents_and_alternative_titles_match(self) -> None:
        cleo = {
            "tmdb_title": "五至七时的克莱奥", "tmdb_original_title": "Cléo de 5 à 7", "original_title": "Cléo from 5 to 7",
            "chinese_title": None, "year": 1962, "tmdb_year": 1962,
        }
        self.assertEqual(candidate_identity(cleo, {"year": "1962"}, "Cleo from 5 to 7 1962 Bluray 1080p x265 10bit FLAC MNHD-FRDS"), (True, None))
        eight = {
            "tmdb_title": "八部半", "tmdb_original_title": "8½", "original_title": "8½",
            "chinese_title": None, "year": 1963, "tmdb_year": 1963,
        }
        title = "Eight and a Half 1963 CC 1080p BluRay x265 10bit FLAC 1.0-ADE"
        self.assertFalse(candidate_identity(eight, {"year": "1963"}, title)[0])
        with_alt = {**eight, "tmdb_alt_titles_json": json.dumps(["Eight and a Half", "Otto e mezzo"])}
        self.assertEqual(candidate_identity(with_alt, {"year": "1963"}, title), (True, None))
        # 其他片名仍受年份约束。
        self.assertFalse(candidate_identity(with_alt, {"year": "1963"}, "Eight and a Half 2019 1080p WEB x265-ADE")[0])

    async def test_alternative_titles_are_fetched_once_and_filtered(self) -> None:
        from app.services.recognition import ensure_alt_titles

        with connect() as conn:
            playlist_id = to_int(conn.execute("INSERT INTO playlists(name,created_at) VALUES('P',?)", (utc_now(),)).lastrowid)
            item_id = to_int(conn.execute(
                """INSERT INTO playlist_items(playlist_id,rank_no,original_title,year,tmdb_id,tmdb_title,tmdb_original_title)
                   VALUES(?,1,'Tropical Malady',2004,9999,'热带疾病','สัตว์ประหลาด')""",
                (playlist_id,),
            ).lastrowid)
            item = conn.execute("SELECT * FROM playlist_items WHERE id=?", (item_id,)).fetchone()
        fetch = AsyncMock(return_value=["Sud Pralad", "สัตว์ประหลาด!", "Tropical Malady", "Mal du tropique"])
        with patch.object(settings, "tmdb_api_key", "test-key"), \
             patch("app.services.recognition.TMDBClient.movie_alternative_titles", new=fetch):
            item = await ensure_alt_titles(item)
            item = await ensure_alt_titles(item)
        fetch.assert_awaited_once()
        # 泰文与已有片名去掉，只留能与种子标题比对的其他片名。
        self.assertEqual(json.loads(item["tmdb_alt_titles_json"]), ["Sud Pralad", "Mal du tropique"])


class ReleaseGroupingTests(IsolatedAppTestCase):
    def test_same_release_across_sites_folds_into_one_candidate(self) -> None:
        from app.services.candidates import present_candidates

        gb = 1024**3
        # 卡萨布兰卡线上数据：同一个 13.22 GB 的 mUHD-FRDS 在各站标题写法不同（附中文介绍、加前缀、词序不同）。
        variants = [
            ("站点A", "Casablanca 1942 2160p UHD BluRay x265 10bit HDR 6Audio mUHD-FRDS 【卡萨布兰卡/北非谍影】国语配音", 38),
            ("站点I", "Casablanca 1942 UHD BluRay 2160p x265 10bit HDR 6Audio mUHD-FRDS", 39),
            ("站点L", "[卡萨布兰卡] Casablanca 1942 2160p UHD BluRay x265 10bit HDR 6Audio mUHD-FRDS", 10),
            ("站点C", "Casablanca 1942 2160p UHD BluRay x265 10bit HDR 6Audio mUHD-FRDS", 12),
        ]
        rows = [{
            "id": f"c{index}", "playlist_item_id": 1, "rank_no": 11, "title": title, "site_name": site,
            "size": int(13.22 * gb) + index * 1024, "seeders": seeders, "group_name": "FRDS", "resolution": "2160p",
            "ranking": index, "eligibility": "eligible", "metadata_json": "{}", "score_breakdown": "[]",
            "detail_url": f"https://{index}.example/details.php?id={index}&passkey=secret", "in_selection": 0,
        } for index, (site, title, seeders) in enumerate(variants)]
        rows.append({**rows[0], "id": "other", "title": "Casablanca.1942.BluRay.1080p.x265.10bit.5Audio.MNHD-FRDS",
                     "size": int(4.52 * gb), "resolution": "1080p", "seeders": 122, "ranking": 9})
        grouped = present_candidates(rows, [])
        self.assertEqual(len(grouped), 2)
        uhd = next(item for item in grouped if item["resolution"] == "2160p")
        # 展示做种最多的一条，其余站点折叠；详情页地址去掉了 passkey。
        self.assertEqual((uhd["site_name"], uhd["seeders"], uhd["site_count"]), ("站点I", 39, 4))
        self.assertEqual(uhd["detail_url"], "https://1.example/details.php?id=1")
        self.assertEqual([option["site_name"] for option in uhd["site_options"]], ["站点I", "站点A", "站点C", "站点L"])
        self.assertTrue(all(option["title"] for option in uhd["site_options"]))


class ConcurrentSearchTests(IsolatedAppTestCase):
    async def test_imdb_hit_skips_title_queries(self) -> None:
        from app.services.search import search_one_site

        site = {"id": 1, "name": "Site", "adapter": "nexusphp", "limit_interval": 0}
        client = AsyncMock()
        client.search = AsyncMock(return_value=[{"title": "Movie.2020.1080p", "imdbid": "tt1"}])
        queries = [("Movie", "tt1", "IMDb tt1"), ("Movie 2020", None, "Movie 2020"), ("电影 2020", None, "电影 2020")]
        item = {"id": 1, "rank_no": 1, "original_title": "Movie"}
        with patch("app.services.search.record_search_attempt"):
            _, torrents, reason, query_count, _ = await search_one_site(
                1, item, site, {"nexusphp": client}, asyncio.Semaphore(1), queries,
                is_target=lambda torrent: torrent.get("imdbid") == "tt1",
            )
        self.assertIsNone(reason)
        self.assertEqual((query_count, len(torrents)), (1, 1))
        # IMDb 只搜到别的影片（站点模糊匹配编号）时，继续按片名检索。
        client.search = AsyncMock(return_value=[{"title": "Other.2012.1080p", "imdbid": "tt9"}])
        with patch("app.services.search.record_search_attempt"):
            _, _, _, query_count, _ = await search_one_site(
                1, item, site, {"nexusphp": client}, asyncio.Semaphore(1), queries,
                is_target=lambda torrent: torrent.get("imdbid") == "tt1",
            )
        self.assertEqual(query_count, 3)

    async def test_transient_site_errors_are_retried(self) -> None:
        import httpx

        from app.services.search import search_one_site

        def http_error(status: int) -> httpx.HTTPStatusError:
            request = httpx.Request("GET", "https://ttg.example/browse.php")
            return httpx.HTTPStatusError("error", request=request, response=httpx.Response(status, request=request))

        site = {"id": 1, "name": "站点A", "adapter": "nexusphp", "limit_interval": 0}
        item = {"id": 1, "rank_no": 1, "original_title": "Movie"}
        queries = [("Movie", None, "Movie")]
        cases = (
            ([http_error(500), [{"title": "Movie.2020.1080p"}]], None, 2),  # 一次 500 后恢复
            ([http_error(500)] * 3, "http_error", 3),  # 重试两次仍失败
            ([http_error(404)], "http_error", 1),  # 404 不是临时错误，不重试
        )
        for effects, expected_code, calls in cases:
            with self.subTest(expected=expected_code, calls=calls):
                client = AsyncMock()
                client.search = AsyncMock(side_effect=effects)
                with patch("app.services.search.record_search_attempt"), \
                     patch("app.services.search.TRANSIENT_RETRY_DELAYS", (0, 0)):
                    _, _, _, _, code = await search_one_site(1, item, site, {"nexusphp": client}, asyncio.Semaphore(1), queries)
                self.assertEqual(code, expected_code)
                self.assertEqual(client.search.await_count, calls)

    async def test_films_search_in_parallel_and_finished_films_leave_searching(self) -> None:
        with connect() as conn:
            playlist_id = to_int(conn.execute("INSERT INTO playlists(name,created_at) VALUES('P',?)", (utc_now(),)).lastrowid)
            item_ids = [to_int(conn.execute(
                "INSERT INTO playlist_items(playlist_id,rank_no,original_title,year,library_state) VALUES(?,?,?,?,'not_found')",
                (playlist_id, rank, f"Movie {rank}", 2020),
            ).lastrowid) for rank in (1, 2, 3, 4)]
            conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at) VALUES('S','nexusphp','https://s.example',1,1,?)",
                (utc_now(),),
            )
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,item_ids_json,created_at,updated_at)
                   VALUES(?,1,4,'queued',4,?,?,?)""",
                (playlist_id, json.dumps(item_ids), utc_now(), utc_now()),
            ).lastrowid)
        running = 0
        peak = 0
        release = asyncio.Event()
        first_done = asyncio.Event()
        searching_while_running: list[set[int]] = []

        async def fake_search_site(_task_id: int, item: object, site: dict, *_args: object, **_kwargs: object) -> tuple:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            if to_int(item["rank_no"]) == 1:  # type: ignore[index]
                running -= 1
                first_done.set()
                return site, [], None, 1, None
            await release.wait()
            running -= 1
            return site, [], None, 1, None

        async def observe() -> None:
            await first_done.wait()
            await asyncio.sleep(0.05)
            from app.services.films import _active_search_items
            with connect() as conn:
                searching_while_running.append(_active_search_items(conn, set(item_ids)))
            release.set()

        media = {"id": 91, "title": "Movie", "original_title": "Movie", "release_date": "2020-01-01"}
        with patch("app.services.search.recognize_item", AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.search_one_site", new=fake_search_site):
            await asyncio.gather(run_search(task_id), observe())
        self.assertEqual(peak, 3)  # 同时最多 3 部：第一部搜完后第四部补上
        # 第一部已搜完不再算“寻片中”；仍在搜和排队的影片仍是。
        self.assertEqual(searching_while_running, [set(item_ids[1:])])
        with connect() as conn:
            task = conn.execute("SELECT status,completed,done_item_ids_json FROM search_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual((task["status"], task["completed"]), ("completed", 4))
        self.assertEqual(sorted(json.loads(task["done_item_ids_json"])), sorted(item_ids))


class SearchQueueGuardTests(IsolatedAppTestCase):
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

    async def test_emby_unknown_blocks_candidate_without_treating_empty_match_as_failure(self) -> None:
        self._seed_selection("emby-unknown")
        with patch.object(TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])), \
             patch("app.api.selection.library_details", new=AsyncMock(return_value=("unknown", None, None))), \
             patch.object(MoviePilotClient, "download", new=AsyncMock()) as download:
            result = await submit_selection()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(result["blocked_unknown"][0]["candidate_id"], "emby-unknown")
        download.assert_not_awaited()
        self.assertEqual([item["id"] for item in await selection()], ["emby-unknown"])

    async def test_search_queue_distinguishes_unknown_from_known_empty_transmission(self) -> None:
        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)",
                ("Queue", 1, utc_now()),
            ).lastrowid)
            conn.execute(
                "INSERT INTO playlist_items(playlist_id,rank_no,original_title,year) VALUES(?,?,?,?)",
                (playlist_id, 1, "Queue Movie", 2020),
            )
        settings.tr_base_url = "http://transmission.example:9091"
        with patch.object(TransmissionClient, "current_downloads", new=AsyncMock(side_effect=RuntimeError("offline"))):
            unknown = await searchable_playlist_items(playlist_id, limit=10)
        self.assertEqual(unknown["download_state"], "unknown")
        self.assertEqual(unknown["items"], [])
        settings.tr_base_url = ""
        from app.services import search as search_module
        search_module._transmission_snapshot_cache.clear()
        with patch.object(TransmissionClient, "current_downloads", new=AsyncMock(return_value=[])):
            empty = await searchable_playlist_items(playlist_id, limit=10)
        self.assertEqual(empty["download_state"], "known_empty")
        self.assertEqual(len(empty["items"]), 1)


class ScorePreviewRouteTests(IsolatedAppTestCase):
    """2-18/2-19：取消任务、通知与设置测试路由的最小行为测试。"""

    async def test_score_preview_route_returns_analysis(self) -> None:
        from app.candidate_policy import DEFAULT_POLICY
        with TestClient(app) as client:
            response = client.post("/api/config/score-preview", json={
                "title": "Movie.2024.1080p.BluRay.x265-FRDS",
                "candidate_policy": DEFAULT_POLICY,
            })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("recommendation", body)
        self.assertIn("ranking", body)
        self.assertEqual(body["group"], "FRDS")


class SiteRateLimitTests(IsolatedAppTestCase):
    async def test_limit_count_allows_burst_then_waits_for_window(self) -> None:
        from app.services.search import wait_for_site_rate_limit

        site = {"id": 4242, "limit_interval": 0.2, "limit_count": 2}
        started = time.monotonic()
        await asyncio.gather(*(wait_for_site_rate_limit(site) for _ in range(3)))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 0.6)


class SiteSearchIntervalTests(IsolatedAppTestCase):
    async def test_search_one_site_respects_limit_interval(self):
        import asyncio
        import time
        from unittest.mock import AsyncMock

        from app.services.search import _site_request_times, search_one_site

        _site_request_times.clear()
        site = {"id": 999, "name": "RateLimitedSite", "adapter": "nexusphp", "limit_interval": 0.05}
        client = AsyncMock()
        client.search = AsyncMock(return_value=[])
        clients = {"nexusphp": client}
        semaphore = asyncio.Semaphore(1)
        queries = [("Query1", None, "Q1"), ("Query2", None, "Q2")]
        item = {"id": 1, "rank_no": 1, "original_title": "Test Movie"}

        start = time.monotonic()
        with patch("app.services.search.record_search_attempt"):
            await search_one_site(1, item, site, clients, semaphore, queries)
        elapsed = time.monotonic() - start

        self.assertEqual(client.search.await_count, 2)
        # 两次检索词之间有 limit_interval=0.05s 延时
        self.assertGreaterEqual(elapsed, 0.04)


class RecognizedTitleSearchTests(IsolatedAppTestCase):
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

    async def test_first_search_accepts_newly_recognized_original_title(self):
        playlist_id, _ = self.create_item("星际穿越", 2014)
        with connect() as conn:
            conn.execute("INSERT INTO pt_sites(name,adapter,created_at) VALUES('Audit','torznab','2026-09-13')")
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,1,1,'queued',1,'2026-09-13','2026-09-13')""", (playlist_id,),
            ).lastrowid

        async def search_site(task, item, site, clients, semaphore, queries, **_kwargs):
            return site, [{"title": "Interstellar.2014.1080p.BluRay.x265-ADE",
                           "site_name": "Audit", "size": 1000000000, "seeders": 10}], None, 1, None

        media = {"id": 157336, "title": "星际穿越", "original_title": "Interstellar", "release_date": "2014-11-07"}
        with patch("app.services.search.recognize_item", new=AsyncMock(return_value=media)), \
             patch("app.services.search.library_details", new=AsyncMock(return_value=("not_found", None, None))), \
             patch("app.services.search.search_one_site", new=search_site):
            await run_search(task_id)
        with connect() as conn:
            rows = conn.execute("SELECT eligibility,exclusion_reason FROM candidates WHERE task_id=?", (task_id,)).fetchall()
        self.assertEqual([(row["eligibility"], row["exclusion_reason"]) for row in rows], [("eligible", None)])
