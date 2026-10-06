"""站点：站点档案与真实页面解析回归（样本已去除账号信息）、站点管理、Cookie 与 CookieCloud、账户统计、从 MoviePilot 同步。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import sites as site_routes
from app.api import system as system_routes
from app.api.sites import add_site, sites, update_site
from app.clients import MTeamClient, NexusPHPClient
from app.config import settings
from app.cookiecloud import cookie_for_host
from app.database import connect, initialize
from app.main import app
from app.outbound import validate_remote_icon_url
from app.schemas import RuntimeSettingsPayload, SitePayload
from app.services.sites import apply_cookie_groups, refresh_stale_site_account_stats, resolve_site_adapter
from app.services.sites import test_site_config as _test_site_config
from app.sites import errors, profile_for
from app.sites.engine import check, search, verify
from app.sites.profiles import PROFILES
from app.sites.official_api_site import parse_results, request_body, search_url
from app.sites.nexusphp import build_params, detect_interruption, parse_page, torrent_deleted
from app.util import raster_image_media_type, to_int, utc_now, volume_factor_value
from tests.support import IsolatedAppTestCase, SeededPlaylistTestCase

# 两个有专用规则的站点域名只在站点档案里出现，测试从档案里取。
ALT_HOST = next(profile.domains[0] for profile in PROFILES if profile.key == "alt_layout")
API_HOST = next(profile.domains[0] for profile in PROFILES if profile.framework == "official_api")
FIXTURES = Path(__file__).parent / "fixtures" / "sites"
NOW = datetime(2026, 9, 28)

# 每个样本的解析结果（第一条种子的关键字段），来自线上《The Godfather》搜索页。
EXPECTED = {
    "tracker-j.example": (7, "The Godfather 1972 1080p BluRay", 29903709798, 14, "tt0068646", "get"),
    "tracker-o.example": (8, "The Last Godfather 2010 NTSC DVD", 7462505676, 5, None, "get"),
    "tracker-i.example": (8, "The Godfather 1972 1080p BluRay", 29903709798, 2, "tt0068646", "get"),
    "tracker-k.example": (8, "Japanese Godfather Ambition 1977", 44560285696, 5, "tt0076461", "post"),
    "tracker-h.example": (8, "Miracles:.The.Canton.Godfather.1", 24910810316, 4, None, "get"),
    "tracker-p.example": (8, "The Godfather Coda The Death of", 32384053411, 2, None, "get"),
    "tracker-f.example": (8, "The Godfather 1972 2160p WEB-DL", 26542897889, 8, None, "get"),
    "tracker-l.example": (8, "[教父3] The Godfather: Part III", 75526999900, 2, None, "get"),
    "tracker-g.example": (8, "The Godfather 1972 1080p BluRay", 29903709798, 5, None, "get"),
    "tracker-q.example": (8, "The Godfather Part III 1990 2160", 20229295964, 0, None, "get"),
    "tracker-n.example": (8, "The Godfather Part III 1990 2160", 20229295964, 1, None, "get"),
    "tracker-c.example": (8, "Japanese Godfather: Conclusion", 43701292236, 1, "tt0077995", "get"),
    "tracker-b.example": (8, "The.Godfather.Part.II.1974.2160p", 86328842649, 2, None, "get"),
    "tracker-a.example": (8, "The Godfather Part III 1990 2160", 20229295964, 0, "tt0099674", "get"),
}


ALT_SAMPLE = "tracker-a.example"  # 需要专用规则的那份样本，按档案里的域名解析


def fixture(host: str) -> str:
    return (FIXTURES / f"{host}.html").read_text(encoding="utf-8")


class RealPageTests(unittest.TestCase):
    def test_every_saved_site_page_parses(self) -> None:
        self.assertEqual({path.stem for path in FIXTURES.glob("*.html")}, set(EXPECTED))
        for host, (count, title, size, seeders, imdb, method) in EXPECTED.items():
            with self.subTest(host=host):
                base = f"https://{ALT_HOST if host == ALT_SAMPLE else host}/"
                rows = parse_page(fixture(host), profile_for(base), base, now=NOW)
                self.assertEqual(len(rows), count)
                first = rows[0]
                self.assertTrue(first.title.startswith(title))
                self.assertEqual((first.size, first.seeders, first.imdb_id, first.download_method), (size, seeders, imdb, method))
                self.assertTrue(all(row.detail_url.startswith(base) and row.download_url.startswith(base) for row in rows))
                self.assertTrue(all(row.size > 0 and row.publish_time for row in rows))

    def test_promotions_become_download_and_upload_factors(self) -> None:
        base = "https://tracker-i.example/"
        first = parse_page(fixture("tracker-i.example"), profile_for(base), base, now=NOW)[0]
        # 此前只认“免费”，50% 折扣会被当成原价。
        self.assertEqual((first.download_factor, first.upload_factor, first.labels), (0.5, 1.0, ["50%"]))
        self.assertEqual(first.leechers, 2)
        self.assertEqual(first.grabs, 35)

    def test_description_is_the_subtitle_line(self) -> None:
        base = "https://tracker-c.example/"
        first = parse_page(fixture("tracker-c.example"), profile_for(base), base, now=NOW)[0]
        self.assertIn("日本的首领", first.description)


class ProfileTests(unittest.TestCase):
    def test_profiles_follow_domains_and_api_keys(self) -> None:
        self.assertEqual(profile_for(f"https://{ALT_HOST}/").key, "alt_layout")
        self.assertEqual(profile_for(f"https://www.{API_HOST}/").key, "nexusphp")
        self.assertEqual(profile_for(f"https://www.{API_HOST}/", has_api_key=True).key, "official_api_site")
        self.assertEqual(profile_for("https://unknown-tracker.example/").key, "nexusphp")

    def test_search_params(self) -> None:
        nexus = profile_for("https://tracker-k.example/")
        self.assertEqual(
            build_params(nexus, "The Godfather", "tt0068646"),
            {"search": "tt0068646", "search_area": 4, "search_mode": 0, "notnewword": 1},
        )
        self.assertEqual(build_params(nexus, "The Godfather", None)["search_area"], 0)
        alt = profile_for(f"https://{ALT_HOST}/")
        # 站点A：search_field，IMDb 写成 imdb0068646（与 MoviePilot 的 imdbid_format 相同）。
        self.assertEqual(build_params(alt, "The Godfather", "tt0068646"), {"c": "M", "search_field": "imdb0068646"})
        self.assertEqual(build_params(alt, "The Godfather", None), {"c": "M", "search_field": "The Godfather"})

    def test_interruptions_are_explained(self) -> None:
        login = '<form action="takelogin.php"><input name="username"><input type="password" name="password"></form>'
        turnstile = login + '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
        cases = (
            ("/login.php", "", errors.CookieExpired),
            ("/torrents.php", login, errors.CookieExpired),
            # 登录表单里嵌 Turnstile 验证码时仍是登录页，不是 Cloudflare 拦截。
            ("/torrents.php", turnstile, errors.CookieExpired),
            ("/take2fa.php", "", errors.TwoFactorRequired),
            ("/claim/", "", errors.SiteMaintenance),
            ("/torrents.php", "<title>Just a moment...</title>", errors.CloudflareChallenge),
            # 站点J：搜索人机验证未通过时页面只有一条错误提示，不能当作“没有结果”。
            ("/torrents.php", (FIXTURES / "interruptions" / "tracker-j.example-search-captcha.html").read_text(encoding="utf-8"), errors.SearchCaptcha),
        )
        for path, html, expected in cases:
            with self.subTest(path=path, html=html[:30]):
                with self.assertRaises(expected):
                    detect_interruption(path, html)
        detect_interruption("/torrents.php", fixture("tracker-k.example"))

    def test_signed_download_links_are_refreshed_from_the_page(self) -> None:
        from app.sites.nexusphp import fresh_signed_download, is_signed_download

        self.assertTrue(is_signed_download("https://tracker-k.example/download.php?id=629570&t=1700000000&sign=abc"))
        self.assertFalse(is_signed_download("https://tracker-b.example/download.php?id=1&passkey=x"))
        # 站点K页面里同一个种子还有打包下载（type=zip）的表单，只取单种下载那个。
        self.assertEqual(
            fresh_signed_download(fixture("tracker-k.example"), "629570", "https://tracker-k.example/"),
            "https://tracker-k.example/download.php?id=629570&t=x&sign=x",
        )
        self.assertIsNone(fresh_signed_download(fixture("tracker-k.example"), "1", "https://tracker-k.example/"))

    def test_unknown_layout_falls_back_to_the_densest_table(self) -> None:
        html = """<table><tr><td>电影</td><td><table><tr><td>
            <a href="details.php?id=42">Movie.2020.1080p.x265-FRDS</a></td></tr></table></td>
            <td><a href="download.php?id=42&amp;passkey=dummy">下载</a></td>
            <td>8.25<br>GiB</td><td>12</td><td>3</td><td>99</td></tr></table>"""
        base = "https://tracker.example/"
        [row] = parse_page(html, profile_for(base), base, now=NOW)
        self.assertEqual((row.title, row.size, row.seeders, row.leechers, row.grabs), ("Movie.2020.1080p.x265-FRDS", int(8.25 * 1024**3), 12, 3, 99))


class OfficialApiTests(unittest.TestCase):
    def test_request_and_results(self) -> None:
        self.assertEqual(search_url(f"https://www.{API_HOST}/"), f"https://api.{API_HOST}/api/v1/torrent/search")
        self.assertEqual(request_body("Cure", "tt0123948")["keyword"], "tt0123948")
        payload = {"data": [{
            "id": 120202, "name": "Cure 1997 1080p BluRay x265-FRDS", "small_descr": "X圣治", "size": 9000,
            "seeders": 3, "leechers": 1, "times_completed": 7, "added": "2026-01-02 03:04:05",
            "promotion_time_type": 2, "downhash": "abc", "imdb_id": "tt0123948", "tmdb_id": 36095,
        }]}
        [torrent] = parse_results(payload, f"https://www.{API_HOST}/")
        self.assertEqual((torrent.download_factor, torrent.labels, torrent.tmdb_id), (0.0, ["FREE"], 36095))
        self.assertEqual(torrent.download_url, f"https://www.{API_HOST}/download.php?id=120202&downhash=abc")
        with self.assertRaisesRegex(errors.ApiError, "密钥无效"):
            parse_results({"error": {"message": "密钥无效"}}, f"https://www.{API_HOST}/")


class EngineTests(IsolatedAppTestCase):
    def _response(self, html: str, url: str) -> httpx.Response:
        return httpx.Response(200, text=html, request=httpx.Request("GET", url))

    async def test_search_returns_candidate_dicts(self) -> None:
        site = {"name": "站点K", "base_url": "https://tracker-k.example", "cookie": "c", "user_agent": "UA"}
        request = AsyncMock(return_value=self._response(fixture("tracker-k.example"), "https://tracker-k.example/torrents.php"))
        with patch("app.sites.engine.safe_request", new=request):
            rows = await search(site, "The Godfather", "tt0068646")
        self.assertEqual(request.await_args.kwargs["params"]["search_area"], 4)
        self.assertEqual(len(rows), 8)
        first = rows[0]
        self.assertEqual((first["site_name"], first["site_ua"], first["download_method"]), ("站点K", "UA", "post"))
        self.assertEqual(first["volume_factor"], 1.0)
        self.assertEqual(first["imdbid"], "tt0076461")

    async def test_official_api_with_api_key_uses_the_api(self) -> None:
        site = {"name": "站点D", "base_url": f"https://www.{API_HOST}", "api_key": "k", "cookie": ""}
        response = httpx.Response(200, json={"data": []}, request=httpx.Request("POST", f"https://api.{API_HOST}/api/v1/torrent/search"))
        request = AsyncMock(return_value=response)
        with patch("app.sites.engine.safe_request", new=request):
            self.assertEqual(await search(site, "Cure"), [])
        call = request.await_args
        self.assertEqual((call.args[1], call.args[2], call.kwargs["headers"]["x-api-key"]), ("POST", f"https://api.{API_HOST}/api/v1/torrent/search", "k"))

    async def test_check_falls_back_to_title_and_reports_empty(self) -> None:
        site = {"name": "站"}
        with patch("app.sites.engine.search", new=AsyncMock(return_value=[])) as searched:
            result = await check(site)
        self.assertEqual([call.args for call in searched.await_args_list], [(site, "The Godfather", "tt0068646"), (site, "The Godfather")])
        self.assertTrue(result["empty"])
        with patch("app.sites.engine.search", new=AsyncMock(return_value=[{"title": "x"}])):
            self.assertNotIn("empty", await check(site))

    async def test_verify_detects_deleted_and_present_torrents(self) -> None:
        site = {"name": "站点K", "base_url": "https://tracker-k.example", "cookie": "c"}
        url = "https://tracker-k.example/details.php?id=1"
        cases = (
            (httpx.Response(404, text="", request=httpx.Request("GET", url)), False),
            (self._response("<p>没有该ID的种子</p><a href='logout.php'>退出</a>", url), False),
            (self._response("<h1>The Godfather</h1><a href='logout.php'>退出</a>", url), True),
        )
        for response, expected in cases:
            request = AsyncMock(return_value=response)
            with patch("app.sites.engine.safe_request", new=request):
                self.assertIs(await verify(site, "details.php?id=1"), expected)
            self.assertEqual((request.await_args.args[2], request.await_args.kwargs["headers"]["Cookie"]), (url, "c"))

    async def test_verify_reports_login_pages_and_skips_unverifiable_sites(self) -> None:
        site = {"name": "站点K", "base_url": "https://tracker-k.example", "cookie": "c"}
        login = self._response("<title>登录</title><form action='takelogin.php'><input type='password'></form>", "https://tracker-k.example/login.php")
        with patch("app.sites.engine.safe_request", new=AsyncMock(return_value=login)):
            with self.assertRaises(errors.CookieExpired):
                await verify(site, "details.php?id=1")
        request = AsyncMock()
        with patch("app.sites.engine.safe_request", new=request):
            # 详情页不在本站时不带 Cookie 外发；官方 API 站点与没有详情页的候选无法确认。
            self.assertIsNone(await verify(site, "https://evil.example/details.php?id=1"))
            self.assertIsNone(await verify(site, None))
            self.assertIsNone(await verify({"base_url": f"https://www.{API_HOST}", "api_key": "k"}, "details.php?id=1"))
        request.assert_not_awaited()
        self.assertFalse(torrent_deleted(200, "<h1>正常详情</h1>"))
        self.assertFalse(torrent_deleted(200, "<td>错误</td><td>你没有该权限！</td>"))
        for message in ("没有该ID的种子", "错误 没有此 ID 的种子。", "沒有該ID的種子", "No torrent with ID 1.", "该种子已被删除"):
            self.assertTrue(torrent_deleted(200, message), message)

    async def test_empty_search_is_recorded_as_its_own_status(self) -> None:
        from app.clients import NexusPHPClient
        from app.services.sites import test_site_config

        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,created_at) VALUES(?,?,?,?,?,?)",
                ("音乐站", "nexusphp", "https://music.example", 1, 1, utc_now()),
            ).lastrowid)
        with patch.object(NexusPHPClient, "check", new=AsyncMock(return_value={"ok": True, "empty": True, "message": "搜不到"})):
            result = await test_site_config({"id": site_id, "name": "音乐站", "adapter": "nexusphp"})
        self.assertEqual((result["status"], result["ok"]), ("empty", False))


class ImdbIdentityTests(unittest.TestCase):
    """站点行带 IMDb 编号时以编号为准。"""

    ITEM = {"tmdb_title": "教父", "tmdb_original_title": "The Godfather", "original_title": "The Godfather",
            "chinese_title": None, "year": 1972, "tmdb_year": 1972, "tmdb_imdb_id": "tt0068646", "imdb_id": None}

    def test_imdb_decides_before_titles(self) -> None:
        from app.domain.titles import candidate_identity

        media = {"year": "1972", "imdb_id": "tt0068646"}
        # 搜《教父》会搜到《日本的首领》：片名都含 Godfather，编号不同即排除。
        self.assertEqual(
            candidate_identity(self.ITEM, media, "Japanese Godfather Ambition 1977 1080p", "tt0076461"),
            (False, "IMDb 编号不匹配：目标 tt0068646，资源为 tt0076461"),
        )
        # 编号一致时不再比对年份与片名（中文命名的发布也能入选）。
        self.assertEqual(candidate_identity(self.ITEM, media, "[教父] 1080p BluRay x265-FRDS", "tt0068646"), (True, None))
        self.assertEqual(
            candidate_identity(self.ITEM, media, "教父I-III合集 The Godfather I-III", "tt0068646"),
            (False, "疑似合集或系列资源"),
        )
        # 没有编号时沿用片名与年份规则。
        self.assertFalse(candidate_identity(self.ITEM, media, "The Godfather 1990 1080p")[0])


class CookieCloudStatusTests(IsolatedAppTestCase):
    async def test_status_requires_server_address_and_reports_pull_interval(self) -> None:
        from app.api.system import cookiecloud_status
        from app.config import settings

        settings.cookiecloud_key, settings.cookiecloud_password = "shared-key", "secret"
        settings.cookiecloud_url = ""
        self.assertFalse((await cookiecloud_status())["configured"])
        settings.cookiecloud_url = "http://cc.example:3000/cookiecloud"
        status = await cookiecloud_status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["pull_interval_minutes"], 10)
        self.assertFalse({"received", "updated_at", "endpoint"} & set(status))


def _insert_site(**values: object) -> int:
    row = {"name": "站点", "adapter": "nexusphp", "base_url": "https://pt.example", "created_at": utc_now()}
    row.update(values)
    columns = ",".join(row)
    placeholders = ",".join("?" for _ in row)
    with connect() as conn:
        return to_int(conn.execute(
            f"INSERT INTO pt_sites({columns}) VALUES({placeholders})", tuple(row.values()),  # nosec B608
        ).lastrowid)

def _adapter(site_id: int) -> str:
    with connect() as conn:
        return str(conn.execute("SELECT adapter FROM pt_sites WHERE id=?", (site_id,)).fetchone()["adapter"])

def response(url: str, *, text: str = "", json_body: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", url)
    if json_body is not None:
        return httpx.Response(200, json=json_body, request=request)
    return httpx.Response(200, text=text, request=request)


class SiteIconAndCookieCloudInputTests(unittest.TestCase):
    def test_only_raster_magic_bytes_are_accepted_for_site_icons(self) -> None:
        self.assertEqual(raster_image_media_type(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertIsNone(raster_image_media_type(b"<svg onload='alert(1)'></svg>"))

    def test_site_icon_fallback_uses_site_name_not_url_scheme(self) -> None:
        fallback = site_routes.site_icon_fallback("云雀", "https://site.example").decode("utf-8")
        self.assertIn(">云雀</text>", fallback)
        self.assertNotIn(">HT</text>", fallback)
        self.assertEqual(site_routes.SITE_ICON_ENDPOINT_VERSION, 2)

        unsafe = site_routes.site_icon_fallback('"<&', "https://example.test").decode("utf-8")
        self.assertIn("aria-label=\"&quot;&lt;\"", unsafe)
        self.assertNotIn('aria-label=""', unsafe)

    def test_cookiecloud_does_not_apply_subdomain_cookie_to_parent_site(self) -> None:
        groups = {
            "example.org": "parent=1",
            "private.example.org": "child=1",
        }
        self.assertEqual(cookie_for_host(groups, "pt.example.org"), ("example.org", "parent=1"))
        self.assertEqual(cookie_for_host(groups, "private.example.org"), ("private.example.org", "child=1"))
        self.assertIsNone(cookie_for_host({"private.example.org": "child=1"}, "example.org"))

class SiteApiTests(SeededPlaylistTestCase):
    async def test_site_connection_records_slow_state_and_duration(self) -> None:
        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,created_at) VALUES(?,?,?,?)",
                ("慢速站点", "nexusphp", "https://slow.example", utc_now()),
            ).lastrowid)
            site = dict(conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone())
        with patch("app.services.sites.time.monotonic", side_effect=[0, 4]), \
             patch("app.services.sites.NexusPHPClient.check", new=AsyncMock(return_value={"ok": True, "message": "连接正常"})):
            result = await _test_site_config(site)
        self.assertEqual(result["status"], "slow")
        self.assertTrue(result["ok"])
        self.assertEqual(result["duration_ms"], 4000)
        with connect() as conn:
            stored = conn.execute("SELECT last_status,last_duration_ms FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        self.assertEqual((stored["last_status"], stored["last_duration_ms"]), ("slow", 4000))

    async def test_site_connection_rejects_explicit_failed_result(self) -> None:
        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,created_at) VALUES(?,?,?,?)",
                ("失败响应站点", "nexusphp", "https://failed.example", utc_now()),
            ).lastrowid)
            site = dict(conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone())
        with patch(
            "app.services.sites.NexusPHPClient.check",
            new=AsyncMock(return_value={"ok": False, "message": "认证失败"}),
        ):
            result = await _test_site_config(site)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "认证失败")

    async def test_local_site_list_does_not_call_moviepilot(self) -> None:
        with connect() as conn:
            site_id = conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,enabled,search_enabled,created_at) VALUES(?,?,?,?,?,?,?)",
                ("独立站点", "nexusphp", "https://tracker.example", "session=secret", 1, 1, utc_now()),
            ).lastrowid
            item_id = conn.execute(
                "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY rank_no LIMIT 1", (self.playlist_id,),
            ).fetchone()[0]
            task_id = conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.playlist_id, 1, 1, "completed", 1, utc_now(), utc_now()),
            ).lastrowid
            conn.execute(
                """INSERT INTO search_attempts(
                       task_id,playlist_item_id,site_id,site_name,status,result_count,duration_ms,created_at,finished_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (task_id, item_id, site_id, "独立站点", "success", 3, 240, utc_now(), utc_now()),
            )
        rows = await site_routes.sites()
        self.assertEqual(rows[-1]["name"], "独立站点")
        self.assertTrue(rows[-1]["cookie_configured"])
        self.assertEqual(rows[-1]["cookie"], "")
        self.assertEqual(rows[-1]["local_stats"]["success_rate"], 100.0)
        self.assertEqual(rows[-1]["local_stats"]["average_ms"], 240)
        self.assertEqual(rows[-1]["local_stats"]["result_count"], 3)
        self.assertNotIn("mp_enhancement", rows[-1])

    async def test_add_site_keeps_url_validation_available_after_route_split(self) -> None:
        payload = site_routes.SitePayload(
            name="新增站点",
            base_url="https://tracker.example",
            cookie="session=test",
            limit_interval=1,
            limit_count=1,
            enabled=True,
            search_enabled=True,
        )
        result = await site_routes.add_site(payload)
        self.assertEqual(result["name"], "新增站点")
        with connect() as conn:
            stored = conn.execute("SELECT base_url FROM pt_sites WHERE id=?", (result["id"],)).fetchone()
        self.assertEqual(stored["base_url"], "https://tracker.example")

    async def test_cookiecloud_updates_local_site_without_moviepilot(self) -> None:
        with connect() as conn:
            site_id = conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,created_at) VALUES(?,?,?,?,?)",
                ("Cookie 站点", "nexusphp", "https://tracker.example", "", utc_now()),
            ).lastrowid
        with patch("app.services.cookiecloud.fetch_cookiecloud", new=AsyncMock(return_value={
            "cookie_data": {
                ".tracker.example": [
                    {"domain": ".tracker.example", "name": "session", "value": "fresh-cookie"},
                ],
            },
        })):
            result = await site_routes.sync_sites_from_cookiecloud()
        with connect() as conn:
            cookie = conn.execute("SELECT cookie FROM pt_sites WHERE id=?", (site_id,)).fetchone()[0]
        self.assertEqual(cookie, "session=fresh-cookie")
        self.assertEqual(result["updated"], 1)

    async def test_single_site_cookie_refresh_uses_autolist_cookiecloud(self) -> None:
        with connect() as conn:
            site_id = conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,user_agent,created_at) VALUES(?,?,?,?,?,?)",
                ("单站刷新", "nexusphp", "https://tracker.example", "old=1", "Local UA", utc_now()),
            ).lastrowid
        with patch("app.services.cookiecloud.fetch_cookiecloud", new=AsyncMock(return_value={
            "cookie_data": {
                ".tracker.example": [
                    {"domain": ".tracker.example", "name": "session", "value": "new-cookie"},
                ],
            },
        })):
            result = await site_routes.refresh_site_cookie(to_int(site_id))
        with connect() as conn:
            row = conn.execute("SELECT cookie,user_agent FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        self.assertTrue(result["ok"])
        self.assertEqual(row["cookie"], "session=new-cookie")
        self.assertEqual(row["user_agent"], "Local UA")

    async def test_cookie_groups_apply_automatically_without_overwriting_user_agent(self) -> None:
        with connect() as conn:
            site_id = to_int(conn.execute(
                """INSERT INTO pt_sites(
                       name,adapter,base_url,cookie,user_agent,account_stats_checked_at,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                ("自动 Cookie", "nexusphp", "https://pt.example.org", "old=1", "Browser UA", utc_now(), utc_now()),
            ).lastrowid)
        result = apply_cookie_groups({"example.org": "session=fresh"})
        with connect() as conn:
            row = conn.execute(
                "SELECT cookie,user_agent,account_stats_checked_at FROM pt_sites WHERE id=?", (site_id,),
            ).fetchone()
        self.assertEqual(result["updated"], ["自动 Cookie"])
        self.assertEqual(row["cookie"], "session=fresh")
        self.assertEqual(row["user_agent"], "Browser UA")
        self.assertIsNone(row["account_stats_checked_at"])

    async def test_pull_decrypts_server_data_and_updates_matching_site(self) -> None:
        settings.cookiecloud_url = "http://moviepilot.local:3000/cookiecloud"
        settings.cookiecloud_key, settings.cookiecloud_password = "pull-test-key-12", "end-to-end-password"
        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,user_agent,created_at) VALUES(?,?,?,?,?,?)",
                ("拉取自动更新", "nexusphp", "https://pull.example", "old=1", "Keep UA", utc_now()),
            ).lastrowid)
        plaintext = json.dumps({
            "cookie_data": {
                ".pull.example": [
                    {"domain": ".pull.example", "name": "session", "value": "pulled-cookie"},
                ],
            },
        }).encode()
        key = hashlib.md5(
            b"pull-test-key-12-end-to-end-password", usedforsecurity=False,
        ).hexdigest()[:16].encode()
        encrypted = base64.b64encode(
            AES.new(key, AES.MODE_CBC, b"\0" * 16).encrypt(pad(plaintext, AES.block_size)),
        ).decode()
        server = AsyncMock(return_value=response(
            "http://moviepilot.local:3000/cookiecloud/get/pull-test-key-12",
            json_body={"encrypted": encrypted, "crypto_type": "aes-128-cbc-fixed"},
        ))
        with patch("app.services.cookiecloud.safe_request", new=server):
            result = await site_routes.sync_sites_from_cookiecloud()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(server.await_args.args[2], "http://moviepilot.local:3000/cookiecloud/get/pull-test-key-12")
        with connect() as conn:
            row = conn.execute("SELECT cookie,user_agent FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        self.assertEqual(row["cookie"], "session=pulled-cookie")
        self.assertEqual(row["user_agent"], "Keep UA")
        # 拉取的数据只用来更新站点，不再写入数据目录。
        self.assertFalse((Path(settings.data_dir) / "cookiecloud").exists())

    async def test_site_account_statistics_are_cached_between_scheduler_runs(self) -> None:
        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,enabled,created_at) VALUES(?,?,?,?,?,?)",
                ("统计站点", "nexusphp", "https://stats.example", "session=1", 1, utc_now()),
            ).lastrowid)
        stats = {"uploaded": 10 * 1024**4, "downloaded": 2 * 1024**4, "ratio": 5.0, "bonus": 123.4, "seeding": 8}
        with patch("app.services.sites.NexusPHPClient.account_stats", new=AsyncMock(return_value=stats)) as mocked:
            first = await refresh_stale_site_account_stats()
            second = await refresh_stale_site_account_stats()
        self.assertTrue(first[0]["ok"])
        self.assertEqual(second, [])
        self.assertEqual(mocked.await_count, 1)
        with connect() as conn:
            row = conn.execute(
                "SELECT account_uploaded,account_downloaded,account_ratio,account_seeding FROM pt_sites WHERE id=?",
                (site_id,),
            ).fetchone()
        self.assertEqual(row["account_uploaded"], stats["uploaded"])
        self.assertEqual(row["account_downloaded"], stats["downloaded"])
        self.assertEqual(row["account_ratio"], 5.0)
        self.assertEqual(row["account_seeding"], 8)

    async def test_site_icon_blocks_cross_host_private_networks(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "内网"):
            await validate_remote_icon_url("http://127.0.0.1/icon.png", "https://example.com")
        await validate_remote_icon_url("http://127.0.0.1/icon.png", "http://127.0.0.1")

    async def test_nexusphp_banner_stats_parses_home_welcome_block(self) -> None:
        """NexusPHP 账户统计优先从首页欢迎横幅解析，不依赖用户详情页。"""
        client = NexusPHPClient()
        banner = client._banner_stats(
            "<b>欢迎回来</b> 上传量：16.605 TB 下载量：1.062 TB "
            "分享率：15.631 魔力值 [ 2,345.67 ] 当前活动： 3 1"
        )
        self.assertIsNotNone(banner)
        assert banner is not None
        self.assertEqual(banner["uploaded"], 18257390579220)
        self.assertEqual(banner["downloaded"], 1167681348698)
        self.assertEqual(banner["ratio"], 15.631)
        self.assertEqual(banner["bonus"], 2345.67)
        self.assertEqual(banner["seeding"], 3)
        # 繁体标签与英文标签同样兼容；无横幅时返回 None。
        alt = client._banner_stats("上傳量: 1.5 TiB 下載量: 200 GiB 分享率: 7.68 做種數: 12")
        self.assertIsNotNone(alt)
        assert alt is not None
        self.assertEqual(alt["ratio"], 7.68)
        self.assertEqual(alt["seeding"], 12)
        self.assertIsNone(client._banner_stats("<html><body>spinner page</body></html>"))

    async def test_tnode_account_stats_from_user_api(self) -> None:
        """TNode SPA 站点（站点T等）从 /api/user/getInfo 计算上传/下载/分享率。"""
        client = NexusPHPClient()
        stats = client._tnode_stats({"upload": 377842816983, "download": 92373125738, "bonus": 774586.57, "seeding": 0})
        self.assertIsNotNone(stats)
        assert stats is not None
        self.assertEqual(stats["uploaded"], 377842816983)
        self.assertEqual(stats["downloaded"], 92373125738)
        self.assertAlmostEqual(float(stats["ratio"] or 0), 377842816983 / 92373125738, places=6)
        self.assertEqual(stats["seeding"], 0)
        # 下载量为 0 时分享率置空（避免除零），上传下载均为 0 时视为无数据。
        zero = client._tnode_stats({"upload": 100, "download": 0})
        self.assertIsNone(zero["ratio"])
        self.assertIsNone(client._tnode_stats({}))

    async def test_nexusphp_publish_time_parses_absolute_and_relative(self) -> None:
        """NexusPHP 时间列：title 里的完整时间、绝对日期（含 / 与中文格式）与存活时间（x月 x天 / 昨天 / 小时）。"""
        import datetime as _dt

        import lxml.html

        from app.sites.nexusphp import _publish_time

        now = _dt.datetime(2026, 9, 28, 12, 0)
        cell = lambda html: lxml.html.fragment_fromstring(f"<td>{html}</td>")
        self.assertEqual(_publish_time(cell('<span title="2026-04-22 10:00:00">5月</span>'), now), "2026-04-22")
        self.assertEqual(_publish_time(cell("2026/04/22"), now), "2026-04-22")
        self.assertEqual(_publish_time(cell("2026年4月22日"), now), "2026-04-22")
        self.assertIsNone(_publish_time(cell("无时间列"), now))
        self.assertEqual(_publish_time(cell("2月 2天"), now), "2026-07-28")
        self.assertEqual(_publish_time(cell("昨天"), now), "2026-09-27")
        self.assertEqual(_publish_time(cell("3 小时"), now), "2026-09-28")


class SiteParsingTests(IsolatedAppTestCase):
    async def test_adapter_selection_and_volume_factor_contracts(self) -> None:
        self.assertEqual(resolve_site_adapter("https://kp.m-team.cc"), "mteam")
        self.assertEqual(resolve_site_adapter("https://indexer.test/api?t=caps"), "torznab")
        self.assertEqual(resolve_site_adapter("https://nexus.example"), "nexusphp")
        self.assertEqual(resolve_site_adapter("https://nexus.example", "https://nexus.example/rss?key=private"), "rss")
        self.assertEqual(resolve_site_adapter("https://nexus.example", "https://nexus.example/rss?key=private", cookie="c_secure=1"), "nexusphp")
        self.assertEqual(resolve_site_adapter("https://tracker-b.example", "https://tracker-b.example/rss", from_moviepilot=True), "nexusphp")
        self.assertEqual(volume_factor_value("FREE"), 0)
        self.assertEqual(volume_factor_value("50%"), 0.5)
        self.assertEqual(volume_factor_value("normal"), 1)

    async def test_nexusphp_decodes_title_and_extracts_binary_size(self) -> None:
        response = Mock()
        response.text = """<table><tr>
          <td>电影</td><td><table><tr><td>预览</td><td>
            <a href="details.php?id=42">&nbsp;Movie.2020.1080p.x265-FRDS</a>
          </td></tr></table></td>
          <td><a href="download.php?id=42&amp;passkey=dummy">下载</a></td>
          <td>8.25<br>GiB</td><td>12</td><td>3</td><td>99</td>
        </tr></table>"""
        response.content = response.text.encode()
        response.url = Mock(path="/torrents.php")
        response.raise_for_status = Mock()
        client = AsyncMock()
        client.get.return_value = response
        # 共享 client 工厂直接返回 AsyncClient 实例（审计 3-24），不再经过 __aenter__。
        with patch("app.clients.httpx.AsyncClient", return_value=client):
            rows = await NexusPHPClient().search(
                {"name": "测试站", "base_url": "https://tracker.example", "cookie": "dummy"},
                "Movie",
                "tt0000042",
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Movie.2020.1080p.x265-FRDS")
        self.assertEqual(rows[0]["size"], to_int(8.25 * 1024**3))
        self.assertEqual(rows[0]["seeders"], 12)

    async def test_nexusphp_account_stats_parse_combined_profile_cell(self) -> None:
        home = Mock()
        home.text = '<a href="userdetails.php?id=42">账户</a>'
        home.content = home.text.encode()
        home.raise_for_status = Mock()
        details = Mock()
        details.text = """<table><tr><td>
          上传量：12.5 TiB<br>下载量：2.5 TiB<br>分享率：5.00<br>魔力值：1234.5<br>做种数：86
        </td></tr></table>"""
        details.content = details.text.encode()
        details.raise_for_status = Mock()
        client = AsyncMock()
        client.get.side_effect = [home, details]
        with patch("app.clients.httpx.AsyncClient", return_value=client):
            stats = await NexusPHPClient().account_stats({
                "name": "测试站",
                "base_url": "https://tracker.example",
                "cookie": "session=dummy",
            })
        self.assertEqual(stats["uploaded"], to_int(12.5 * 1024**4))
        self.assertEqual(stats["downloaded"], to_int(2.5 * 1024**4))
        self.assertEqual(stats["ratio"], 5.0)
        self.assertEqual(stats["seeding"], 86)


class RSSBadEnclosureTests(IsolatedAppTestCase):
    async def test_rss_client_tolerates_non_numeric_enclosure_length(self) -> None:
        from app.clients import RSSClient

        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><item>
          <title>Movie 2020 1080p WEB-DL</title>
          <enclosure url="https://tracker.example/detail/torrent.torrent" length="not-a-number"/>
        </item><item>
          <title>Movie 2020 2160p REMUX</title>
          <enclosure url="https://tracker.example/detail/torrent2.torrent" length="1.5GB"/>
        </item><item>
          <title>Movie 2020 720p</title>
          <enclosure url="https://tracker.example/detail/torrent3.torrent"/>
        </item></channel></rss>"""
        response = Mock()
        response.status_code = 200
        response.content = xml
        response.headers = {}
        response.raise_for_status = Mock()
        site = {
            "name": "RSS 测试站", "rss_url": "https://feed.example/rss.xml", "user_agent": "AutoList",
            "cookie": "", "timeout_seconds": 30,
        }
        with patch("app.clients.safe_request", new=AsyncMock(return_value=response)) as request:
            results = await RSSClient().search(site, "Movie 2020")
        request.assert_awaited_once()
        self.assertEqual(len(results), 3)
        self.assertEqual([item["size"] for item in results], [0, 0, 0])
        self.assertEqual(results[0]["site_name"], "RSS 测试站")


class DuplicateSiteNameTests(IsolatedAppTestCase):
    async def test_duplicate_site_name_returns_409_not_500(self) -> None:
        payload = {"name": "重复站点", "base_url": "https://tracker.example"}
        with TestClient(app) as client:
            first = client.post("/api/sites", json=payload)
            second = client.post("/api/sites", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"], "站点名称已存在")

    async def test_rename_to_existing_name_returns_409(self) -> None:
        with TestClient(app) as client:
            first = client.post("/api/sites", json={"name": "站点甲", "base_url": "https://tracker-a.example"})
            second = client.post("/api/sites", json={"name": "站点乙", "base_url": "https://tracker-b.example"})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            site_id = second.json()["id"]
            renamed = client.put(
                f"/api/sites/{site_id}",
                json={"name": "站点甲", "base_url": "https://tracker-b.example", "clear_api_key": False, "clear_cookie": False, "clear_rss_url": False},
            )
        self.assertEqual(renamed.status_code, 409)


class SiteAdapterMigrationTests(IsolatedAppTestCase):
    async def test_rss_only_site_survives_restarts(self) -> None:
        site_id = _insert_site(name="纯 RSS", adapter="rss", base_url="https://example.org", rss_url="https://example.org/feed.xml")
        initialize()
        initialize()
        self.assertEqual(_adapter(site_id), "rss")

    async def test_upgrade_recomputes_adapters_once(self) -> None:
        wrong_rss = _insert_site(name="有 Cookie 的 RSS", adapter="rss", base_url="https://a.example", rss_url="https://a.example/rss", cookie="uid=1")
        broken_rss = _insert_site(name="被误改的 RSS", adapter="nexusphp", base_url="https://b.example", rss_url="https://b.example/rss")
        plain = _insert_site(name="普通站点", adapter="nexusphp", base_url="https://c.example", cookie="uid=2")
        mteam = _insert_site(name="馒头", adapter="mteam", base_url="https://kp.m-team.cc", rss_url="https://kp.m-team.cc/rss")
        with connect() as conn:
            # 模拟 1.43（schema v3）数据库升级：只有 v3→v4 迁移会重算适配器。
            conn.execute("PRAGMA user_version=3")
        initialize()
        self.assertEqual(_adapter(wrong_rss), "nexusphp")
        self.assertEqual(_adapter(broken_rss), "rss")
        self.assertEqual(_adapter(plain), "nexusphp")
        self.assertEqual(_adapter(mteam), "mteam")
        # 迁移只执行一次：之后用户手动选择的状态不会在重启时被再次改写。
        with connect() as conn:
            conn.execute("UPDATE pt_sites SET adapter='nexusphp' WHERE id=?", (broken_rss,))
        initialize()
        self.assertEqual(_adapter(broken_rss), "nexusphp")

    async def test_identical_cookie_is_not_rewritten(self) -> None:
        from app.services.sites import apply_cookie_groups

        checked_at = utc_now()
        site_id = _insert_site(
            name="Cookie 未变化", base_url="https://pt.same.example", cookie="session=1",
            account_stats_checked_at=checked_at,
        )
        result = apply_cookie_groups({"same.example": "session=1"})
        self.assertEqual((result["updated"], result["unchanged"]), ([], ["Cookie 未变化"]))
        with connect() as conn:
            row = conn.execute("SELECT account_stats_checked_at FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        # 账户统计缓存不能因为每小时的同步被重置。
        self.assertEqual(row["account_stats_checked_at"], checked_at)

    async def test_identical_cookie_still_records_cookiecloud_as_the_source(self) -> None:
        from app.services.sites import apply_cookie_groups

        site_id = _insert_site(name="手动填过同一份", base_url="https://pt.same.example", cookie="session=1")
        with connect() as conn:
            conn.execute("UPDATE pt_sites SET cookie_source=NULL,cookie_updated_at=NULL,last_status='ok' WHERE id=?", (site_id,))
        apply_cookie_groups({"same.example": "session=1"})
        with connect() as conn:
            row = conn.execute("SELECT cookie_source,cookie_updated_at,last_status FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        self.assertEqual((row["cookie_source"], row["last_status"]), ("cookiecloud", "ok"))
        self.assertIsNotNone(row["cookie_updated_at"])

    async def test_cookie_from_cookiecloud_switches_rss_site_to_nexusphp(self) -> None:
        from app.services.sites import apply_cookie_groups

        site_id = _insert_site(name="RSS 转页面", adapter="rss", base_url="https://pt.rss.example", rss_url="https://pt.rss.example/rss")
        apply_cookie_groups({"rss.example": "session=1"})
        self.assertEqual(_adapter(site_id), "nexusphp")


class CookieCloudKeyTests(IsolatedAppTestCase):
    async def test_settings_validate_the_key_format(self) -> None:
        RuntimeSettingsPayload(cookiecloud_key="abcde")
        for invalid in ("abcd", "bad/key"):
            with self.subTest(key=invalid):
                with self.assertRaises(ValidationError):
                    RuntimeSettingsPayload(cookiecloud_key=invalid)

    async def test_status_endpoint_never_returns_url_secrets(self) -> None:
        from app.api.system import cookiecloud_status

        settings.cookiecloud_url = "http://cc.example:8088/cookiecloud?token=SECRET_VALUE"
        status = await cookiecloud_status()
        self.assertNotIn("SECRET_VALUE", status["url"])
        self.assertTrue(status["url_configured"])


class MoviePilotSyncAndStatsTests(IsolatedAppTestCase):
    async def test_moviepilot_sync_preserves_local_choices_and_skips_collisions(self) -> None:
        from app.services.sites import sync_sites_from_moviepilot

        local_id = _insert_site(
            name="本地站点K", base_url="https://tracker-k.example", priority=7, enabled=1, search_enabled=0,
            user_agent="My UA", cookie="old=1", timeout_seconds=45,
        )
        renamed_domain = _insert_site(name="站点Q", base_url="https://old-domain.example", priority=9)
        remote = [
            {"name": "站点K", "url": "https://tracker-k.example/", "cookie": "new=1", "pri": 1, "ua": "MP UA", "is_active": True, "timeout": 15},
            {"name": "站点Q", "url": "https://tracker-q.example/", "cookie": "c=1", "pri": 2},
            {"name": "坏地址", "url": "ftp://bad.example/"},
            {"name": "新站", "url": "https://new.example/", "cookie": "n=1", "apikey": "k", "pri": 5, "timeout": 15},
            {"name": "新站镜像", "url": "https://new.example/", "cookie": "n=2"},
        ]
        with patch("app.services.sites.fetch_moviepilot_sites", new=AsyncMock(return_value=remote)):
            result = await sync_sites_from_moviepilot()
        with connect() as conn:
            local = conn.execute("SELECT * FROM pt_sites WHERE id=?", (local_id,)).fetchone()
            new_sites = conn.execute("SELECT * FROM pt_sites WHERE base_url='https://new.example'").fetchall()
            moved = conn.execute("SELECT * FROM pt_sites WHERE id=?", (renamed_domain,)).fetchone()
        self.assertEqual(
            (local["name"], local["priority"], local["search_enabled"], local["user_agent"], local["cookie"], local["timeout_seconds"]),
            ("本地站点K", 7, 0, "My UA", "new=1", 45),
        )
        self.assertEqual(len(new_sites), 1)
        # MoviePilot 默认的 15 秒对 AutoList 太紧，新站点至少 30 秒。
        self.assertEqual((new_sites[0]["api_key"], new_sites[0]["timeout_seconds"]), ("k", 30))
        # 同名站点视为更换了域名：更新地址，但保留本地优先级。
        self.assertEqual((moved["base_url"], moved["priority"]), ("https://tracker-q.example", 9))
        self.assertEqual(result["skipped"], ["坏地址（地址无效）"])

    async def test_account_stats_without_credentials_skip_network(self) -> None:
        from app.services.sites import refresh_site_account_stats

        site_id = _insert_site(name="无 Cookie", cookie="")
        with connect() as conn:
            site = dict(conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone())
        with patch("app.services.sites.NexusPHPClient.account_stats", new=AsyncMock()) as stats:
            result = await refresh_site_account_stats(site)
        stats.assert_not_awaited()
        self.assertFalse(result["ok"])
        self.assertIn("未配置 Cookie", result["error"])

    async def test_site_errors_are_translated(self) -> None:
        from app.services.sites import friendly_site_error

        request = httpx.Request("GET", "https://pt.example/userdetails.php?passkey=abc")
        error = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        message = friendly_site_error(error)
        self.assertIn("HTTP 404", message)
        self.assertNotIn("developer.mozilla.org", message)
        self.assertNotIn("passkey", message)


class DiscountTests(IsolatedAppTestCase):
    async def test_mteam_double_upload_discounts(self) -> None:
        self.assertEqual(MTeamClient._discount_factor("_2X_FREE"), 0.0)
        self.assertEqual(MTeamClient._discount_factor("_2X_PERCENT_50"), 0.5)
        self.assertEqual(MTeamClient._discount_factor("_2X"), 1.0)
        row = MTeamClient()._parse_row(
            {"id": 1, "name": "Movie", "status": {"discount": "_2X_FREE"}},
            {"name": "M-Team", "base_url": "https://kp.m-team.cc", "api_key": "k"},
        )
        self.assertEqual(row["volume_factor"], 0.0)
        self.assertEqual(row["labels"][:2], ["2X", "FREE"])


class CookieCloudAndRssTests(IsolatedAppTestCase):
    async def test_private_rss_can_be_saved_replaced_and_remains_hidden(self):
        first = "https://example.org/rss?passkey=audit-first"
        created = await add_site(SitePayload(name="Audit RSS", base_url="https://example.org", rss_url=first))
        second = "https://example.org/rss?key=audit-second"
        await update_site(created["id"], SitePayload(name="Audit RSS", base_url="https://example.org", rss_url=second))
        with connect() as conn:
            row = conn.execute("SELECT rss_url FROM pt_sites WHERE id=?", (created["id"],)).fetchone()
        self.assertEqual(row["rss_url"], second)
        public = (await sites())[0]
        self.assertEqual(public["rss_url"], "")
        self.assertTrue(public["rss_url_configured"])

    async def test_rss_retains_scheme_userinfo_and_private_host_restrictions(self):
        for url in ("file:///tmp/feed", "https://user:password@example.org/rss", "http://127.0.0.1/rss?key=audit"):
            with self.subTest(url=url), patch.dict(os.environ, {"AUTOLIST_ACCESS_TOKEN": "audit-enabled", "AUTOLIST_ALLOW_PRIVATE_HOSTS": ""}):
                with self.assertRaises(HTTPException) as caught:
                    await add_site(SitePayload(name="Invalid RSS", base_url="https://8.8.8.8", rss_url=url))
                self.assertEqual(caught.exception.status_code, 422)

    def test_push_receiver_is_removed_and_not_exempt_from_the_access_token(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, raise_server_exceptions=False)
        for method, path in (("POST", "/cookiecloud/update"), ("POST", "/update"), ("GET", "/cookiecloud/get/abcde"), ("GET", "/get/abcde")):
            with self.subTest(path=path):
                res = client.request(method, path, json={} if method == "POST" else None)
                self.assertIn(res.status_code, (404, 405))
                self.assertIsNone(res.headers.get("access-control-allow-origin"))
        with patch.dict(os.environ, {"AUTOLIST_ACCESS_TOKEN": "A9b8C7d6" * 4}):
            self.assertEqual(client.get("/cookiecloud/").status_code, 401)

    async def test_cookiecloud_operations_and_event_logging(self):
        from unittest.mock import patch

        from app.api import sites as site_routes
        from app.database import connect
        from app.logs import event_logger
        from app.util import to_int, utc_now

        with connect() as conn:
            site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,user_agent,created_at) VALUES(?,?,?,?,?,?)",
                ("日志测试站点", "nexusphp", "https://test-log.org", "old=1", "TestUA", utc_now()),
            ).lastrowid)

        try:
            mock_payload = {"cookie_data": {".test-log.org": [{"domain": ".test-log.org", "name": "c_session", "value": "testlogcookieval"}]}}
            with patch("app.services.cookiecloud.fetch_cookiecloud", new=AsyncMock(return_value=mock_payload)):
                with patch.object(event_logger(), "info") as mock_info:
                    sync_res = await site_routes.sync_sites_from_cookiecloud()
                    self.assertTrue(sync_res["ok"])
                    mock_info.assert_called()
                    call_args = mock_info.call_args
                    self.assertEqual(call_args[0][0], "cookiecloud_sync")
                    self.assertIn("detail", call_args[1].get("extra", {}))

                with patch.object(event_logger(), "info") as mock_info:
                    refresh_res = await site_routes.refresh_site_cookie(site_id)
                    self.assertTrue(refresh_res["ok"])
                    mock_info.assert_called()
                    call_args = mock_info.call_args
                    self.assertEqual(call_args[0][0], "site_cookie_refreshed")
                    self.assertIn("detail", call_args[1].get("extra", {}))
        finally:
            with connect() as conn:
                conn.execute("DELETE FROM pt_sites WHERE id=?", (site_id,))

    async def test_cookiecloud_remote_sync_and_test_provider(self) -> None:
        from app.api import sites as site_routes
        from app.api import system as system_routes
        from app.config import settings

        orig_url = settings.cookiecloud_url
        orig_key = settings.cookiecloud_key
        orig_pass = settings.cookiecloud_password
        try:
            settings.cookiecloud_url = "http://192.0.2.10:3000/cookiecloud"
            settings.cookiecloud_key = "test-key"
            settings.cookiecloud_password = "test-pass"

            mock_payload = {
                "cookie_data": {
                    ".tracker-k.example": [{"domain": ".tracker-k.example", "name": "c_secure_uid", "value": "test_uid"}]
                }
            }
            with patch("app.services.cookiecloud.fetch_cookiecloud", new=AsyncMock(return_value=mock_payload)) as mock_fetch, \
                 patch("app.api.system.fetch_cookiecloud", new=AsyncMock(return_value=mock_payload)):
                res = await site_routes.sync_sites_from_cookiecloud()
                self.assertIn("message", res)
                mock_fetch.assert_awaited_once_with()

                test_res = await system_routes.test_runtime_settings("cookiecloud")
                self.assertIn("cookiecloud", test_res)
                self.assertTrue(test_res["cookiecloud"]["ok"])
        finally:
            settings.cookiecloud_url = orig_url
            settings.cookiecloud_key = orig_key
            settings.cookiecloud_password = orig_pass

    async def test_sync_sites_from_moviepilot_and_cookiecloud_auto_import(self) -> None:
        from app.api import sites as site_routes

        mock_mp_sites = [
            {"id": 1, "name": "站点K", "url": "https://tracker-k.example/", "cookie": "c_uid=1", "pri": 1, "is_active": True},
            {"id": 2, "name": "站点Q", "url": "https://tracker-q.example/", "cookie": "", "pri": 2, "is_active": True},
            {"id": 3, "name": "站点Q", "url": "https://tracker-q.example/", "cookie": "ptchd=1", "pri": 3, "is_active": True},
        ]
        with patch("app.services.sites.fetch_moviepilot_sites", new_callable=AsyncMock) as mock_sites:
            mock_sites.return_value = mock_mp_sites
            res = await site_routes.sync_sites_from_mp()
            self.assertTrue(res["ok"])
            self.assertEqual(res["total"], 3)
            with connect() as conn:
                rows = conn.execute("SELECT name, base_url, cookie FROM pt_sites ORDER BY id").fetchall()
                names = [r["name"] for r in rows]
                self.assertIn("站点K", names)
                # Disambiguation happened for duplicates
                self.assertTrue(any("tracker-q.example" in n for n in names))
                self.assertTrue(any("tracker-q.example" in n for n in names))
                conn.execute("DELETE FROM pt_sites")


class TrackerAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mteam_missing_code_with_error_message_is_rejected(self) -> None:
        upstream = response(
            "https://tracker.example/api/torrent/search",
            json_body={"message": "INVALID_TOKEN", "data": {"data": []}},
        )
        site = {
            "name": "M-Team",
            "base_url": "https://tracker.example",
            "api_key": "configured-test-key",
            "timeout_seconds": 5,
        }
        with patch("app.clients.safe_request", new=AsyncMock(return_value=upstream)):
            with self.assertRaisesRegex(RuntimeError, "INVALID_TOKEN"):
                await MTeamClient().search(site, "AutoListConnectionProbe")

    async def test_nexus_login_page_is_not_reported_as_empty_search(self) -> None:
        upstream = response(
            "https://tracker.example/login.php",
            text="""
                <html><body>
                  <form action="takelogin.php" method="post">
                    <input name="username">
                    <input name="password" type="password">
                  </form>
                </body></html>
            """,
        )
        site = {
            "name": "Nexus",
            "base_url": "https://tracker.example",
            "cookie": "expired=test",
            "timeout_seconds": 5,
        }
        with patch("app.sites.engine.safe_request", new=AsyncMock(return_value=upstream)):
            with self.assertRaisesRegex(RuntimeError, "Cookie 已失效"):
                await NexusPHPClient().search(site, "AutoListConnectionProbe")


class CookieProvenanceTests(IsolatedAppTestCase):
    """Cookie 的来源与更新时间、最近一次同步摘要，以及 Cookie 变化后的后台复测。"""

    def _site(self, name: str, base: str, cookie: str = "old") -> int:
        with connect() as conn:
            return to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,enabled,search_enabled,created_at) VALUES(?,?,?,?,?,?,?)",
                (name, "nexusphp", base, cookie, 1, 1, utc_now()),
            ).lastrowid)

    async def test_sync_records_source_summary_and_retests_changed_sites(self) -> None:
        from app import state as app_state
        from app.services import sites as site_service

        changed = self._site("站点B", "https://tracker-b.example")
        same = self._site("站点I", "https://tracker-i.example", cookie="same")
        self._site("站点R", "https://tracker-r.example")
        groups = {"tracker-b.example": "new", "tracker-i.example": "same"}
        with patch.object(site_service, "test_site_config", new=AsyncMock()) as retest:
            result = site_service.apply_cookie_groups(groups, origin="schedule")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.assertEqual((result["updated"], result["missing"]), (["站点B"], ["站点R"]))
        with connect() as conn:
            rows = {r["id"]: r for r in conn.execute("SELECT id,cookie_source,cookie_updated_at FROM pt_sites")}
        self.assertEqual(rows[changed]["cookie_source"], "cookiecloud")
        self.assertIsNotNone(rows[changed]["cookie_updated_at"])
        self.assertEqual(rows[same]["cookie_source"], "cookiecloud")
        summary = app_state.last_cookie_sync
        self.assertEqual((summary["origin"], summary["updated"], summary["unchanged"], summary["missing"]), ("schedule", ["站点B"], 1, ["站点R"]))
        # 只复测 Cookie 有变化的站点。
        self.assertEqual([call.args[0]["id"] for call in retest.await_args_list], [changed])
        status = await system_routes.cookiecloud_status()
        self.assertEqual(status["last_sync"]["origin"], "schedule")

    async def test_manual_cookie_edit_marks_source(self) -> None:
        from app.main import app

        site_id = self._site("站点B", "https://tracker-b.example")
        body = {"name": "站点B", "base_url": "https://tracker-b.example", "cookie": "typed-by-user"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(f"/api/sites/{site_id}", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        with connect() as conn:
            row = conn.execute("SELECT cookie_source,cookie_updated_at FROM pt_sites WHERE id=?", (site_id,)).fetchone()
        self.assertEqual(row["cookie_source"], "manual")
        self.assertIsNotNone(row["cookie_updated_at"])


class CookieCloudPullTests(IsolatedAppTestCase):
    """定时拉取的间隔，以及站点报 Cookie 已失效时补拉 CookieCloud 并重试。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        settings.cookiecloud_url = "http://moviepilot.local:3000/cookiecloud"
        settings.cookiecloud_key, settings.cookiecloud_password = "shared-key", "secret"
        with connect() as conn:
            self.site_id = to_int(conn.execute(
                "INSERT INTO pt_sites(name,adapter,base_url,cookie,enabled,search_enabled,created_at) VALUES(?,?,?,?,?,?,?)",
                ("站点B", "nexusphp", "https://tracker-b.example", "session=old", 1, 1, utc_now()),
            ).lastrowid)
        self.site = {"id": self.site_id, "name": "站点B", "cookie": "session=old"}

    @staticmethod
    def _cloud(value: str) -> AsyncMock:
        return AsyncMock(return_value={
            "cookie_data": {".tracker-b.example": [{"domain": ".tracker-b.example", "name": "session", "value": value}]},
        })

    async def test_scheduled_pull_is_due_every_ten_minutes(self) -> None:
        from app.services import cookiecloud

        self.assertTrue(cookiecloud.pull_due())
        with patch.object(cookiecloud, "fetch_cookiecloud", new=self._cloud("new")), \
             patch("app.services.sites.test_site_config", new=AsyncMock()):
            await cookiecloud.pull_cookiecloud("schedule")
        self.assertFalse(cookiecloud.pull_due())
        cookiecloud._last_pull_monotonic -= cookiecloud.PULL_INTERVAL_SECONDS
        self.assertTrue(cookiecloud.pull_due())

    async def test_expired_cookie_is_refreshed_and_the_request_retried_once(self) -> None:
        from app import state as app_state
        from app.services import cookiecloud
        from app.sites.errors import CookieExpired

        seen: list[str] = []

        async def request(site: dict) -> str:
            seen.append(site["cookie"])
            if site["cookie"] == "session=old":
                raise CookieExpired()
            return "results"

        with patch.object(cookiecloud, "fetch_cookiecloud", new=self._cloud("new")) as fetch, \
             patch("app.services.sites.test_site_config", new=AsyncMock()):
            self.assertEqual(await cookiecloud.with_cookie_refresh(self.site, request), "results")
        self.assertEqual(seen, ["session=old", "session=new"])
        self.assertEqual(self.site["cookie"], "session=new")
        fetch.assert_awaited_once()
        self.assertEqual(app_state.last_cookie_sync["origin"], "expired")
        with connect() as conn:
            self.assertEqual(conn.execute("SELECT cookie FROM pt_sites WHERE id=?", (self.site_id,)).fetchone()[0], "session=new")

    async def test_expired_cookie_without_a_newer_one_is_not_pulled_again_within_the_cooldown(self) -> None:
        from app.services import cookiecloud
        from app.sites.errors import CookieExpired

        request = AsyncMock(side_effect=CookieExpired())
        with patch.object(cookiecloud, "fetch_cookiecloud", new=self._cloud("old")) as fetch:
            for _ in range(3):
                with self.assertRaises(CookieExpired):
                    await cookiecloud.with_cookie_refresh(dict(self.site), request)
        # CookieCloud 里也是旧 Cookie：第一次补拉后进入冷却期，后面两次不再拉取，也不重试。
        fetch.assert_awaited_once()
        self.assertEqual(request.await_count, 3)

    async def test_expired_cookie_is_not_refreshed_without_cookiecloud(self) -> None:
        from app.services import cookiecloud
        from app.sites.errors import CookieExpired

        settings.cookiecloud_url = ""
        with patch.object(cookiecloud, "fetch_cookiecloud", new=self._cloud("new")) as fetch:
            with self.assertRaises(CookieExpired):
                await cookiecloud.with_cookie_refresh(self.site, AsyncMock(side_effect=CookieExpired()))
        fetch.assert_not_awaited()

    async def test_site_search_uses_the_refreshed_cookie(self) -> None:
        import sqlite3

        from app.services import cookiecloud
        from app.services.search import search_one_site
        from app.sites.errors import CookieExpired

        with connect() as conn:
            playlist_id = to_int(conn.execute(
                "INSERT INTO playlists(name,position,created_at) VALUES(?,?,?)", ("片单", 1, utc_now()),
            ).lastrowid)
            item_id = to_int(conn.execute(
                "INSERT INTO playlist_items(playlist_id,rank_no,original_title,year) VALUES(?,?,?,?)",
                (playlist_id, 1, "Cure", 1997),
            ).lastrowid)
            task_id = to_int(conn.execute(
                """INSERT INTO search_tasks(playlist_id,range_start,range_end,status,total,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""", (playlist_id, 1, 1, "running", 1, utc_now(), utc_now()),
            ).lastrowid)
            conn.row_factory = sqlite3.Row
            item = conn.execute("SELECT * FROM playlist_items WHERE id=?", (item_id,)).fetchone()
        site = {**self.site, "adapter": "nexusphp", "priority": 1, "limit_interval": 0, "limit_count": 0}

        class Client:
            async def search(self, current: dict, title: str, imdb_id: str | None) -> list[dict]:
                if current["cookie"] == "session=old":
                    raise CookieExpired()
                return [{"title": "Cure.1997.1080p.BluRay", "size": 1, "enclosure": "https://tracker-b.example/dl/1"}]

        with patch.object(cookiecloud, "fetch_cookiecloud", new=self._cloud("new")), \
             patch("app.services.sites.test_site_config", new=AsyncMock()):
            _, torrents, reason, _, _ = await search_one_site(
                task_id, item, site, {"nexusphp": Client()}, asyncio.Semaphore(1), [("Cure", None, "片名")],
            )
        self.assertIsNone(reason)
        self.assertEqual(len(torrents), 1)
        self.assertEqual(site["cookie"], "session=new")


class SearchCaptchaLinkTests(IsolatedAppTestCase):
    async def test_search_captcha_gives_a_verify_link(self) -> None:
        from app.sites.errors import SEARCH_CAPTCHA_MESSAGE

        with connect() as conn:
            conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,last_status,last_message,created_at)
                   VALUES('站点J','nexusphp','https://tracker-j.example/',1,1,'error',?,?)""",
                (SEARCH_CAPTCHA_MESSAGE, utc_now()),
            )
            conn.execute(
                """INSERT INTO pt_sites(name,adapter,base_url,enabled,search_enabled,last_status,last_message,created_at)
                   VALUES('其他','nexusphp','https://other.example/',1,1,'error','站点连接失败',?)""",
                (utc_now(),),
            )
        links = {site["name"]: site["verify_url"] for site in await sites()}
        self.assertEqual(links, {"站点J": "https://tracker-j.example/torrents.php", "其他": None})


class CookieCloudFailureMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_test_never_returns_the_user_key(self) -> None:
        from fastapi import HTTPException

        from app.services import cookiecloud

        key = "SyntheticKey_abc123"
        request = httpx.Request("GET", f"http://cc.example:8088/cookiecloud/get/{key}")
        response = httpx.Response(503, request=request)
        with patch.object(settings, "cookiecloud_url", "http://cc.example:8088/cookiecloud"), \
             patch.object(settings, "cookiecloud_key", key), patch.object(settings, "cookiecloud_password", "pw"), \
             patch("app.services.cookiecloud.safe_request", new=AsyncMock(return_value=response)):
            with self.assertRaises(HTTPException) as raised:
                await cookiecloud.fetch_cookiecloud()
        self.assertNotIn(key, str(raised.exception.detail))
        self.assertIn("503", str(raised.exception.detail))
