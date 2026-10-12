"""MoviePilot：提交下载、下载与整理历史、按种子名识别。"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..config import settings
from ..outbound import safe_request
from ..util import to_float, to_int
from .tmdb import TMDBClient


class MoviePilotClient:
    """MoviePilot gateway kept only for download classification and organization."""

    def __init__(self) -> None:
        self.base_url = settings.mp_base_url
        self.headers = {"X-API-KEY": settings.mp_api_key} if settings.mp_api_key else {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=settings.mp_timeout_seconds, follow_redirects=False)

    async def check(self) -> dict[str, Any]:
        if not self.base_url or not settings.mp_api_key:
            return {"ok": False, "configured": False, "message": "未配置 MoviePilot"}
        async with self._client() as client:
            response = await safe_request(
                client, "GET", "/api/v1/download/clients", headers=self.headers, label="MoviePilot 地址", allow_private=True,
            )
            response.raise_for_status()
            return {"ok": True, "configured": True, "downloaders": response.json()}

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        async with self._client() as client:
            response = await safe_request(
                client, "GET", path, headers=self.headers, params=params, label="MoviePilot 地址", allow_private=True,
            )
            response.raise_for_status()
            body = response.json()
        return body.get("data") if isinstance(body, dict) and "data" in body else body

    async def download_history(self, page: int, count: int) -> list[dict[str, Any]]:
        """MoviePilot 的下载历史（按时间倒序）：种子 hash、片名、年份、海报与站点。"""
        data = await self._get("/api/v1/history/download", {"page": page, "count": count})
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def transfer_history(self, page: int, count: int) -> list[dict[str, Any]]:
        """MoviePilot 的整理历史（按时间倒序）：种子 hash、是否成功与失败原因。"""
        data = await self._get("/api/v1/history/transfer", {"page": page, "count": count})
        if isinstance(data, dict):
            data = data.get("list") or []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def recognize(self, title: str) -> dict[str, Any] | None:
        """按种子名识别影片（MoviePilot 的识别词与 TMDB 识别），认不出返回 None。"""
        data = await self._get("/api/v1/media/recognize", {"title": title})
        media = data.get("media_info") if isinstance(data, dict) else None
        return media if isinstance(media, dict) and media.get("title") else None

    async def transmission_downloader(self) -> str:
        """MoviePilot 里 Transmission 下载器的名称（用户自己起的，如 TR）。提交时必须用这个名称，否则 MP 报“未找到下载器”。"""
        status = await self.check()
        if not status.get("ok"):
            raise RuntimeError(str(status.get("message") or "无法连接 MoviePilot"))
        clients = status.get("downloaders")
        if isinstance(clients, dict):
            clients = clients.get("data") if isinstance(clients.get("data"), list) else []
        options = [
            item for item in (clients or [])
            if isinstance(item, dict) and str(item.get("type") or "").lower() == "transmission" and item.get("name")
            and item.get("enabled", True) is not False
        ]
        if not options:
            raise RuntimeError("MoviePilot 里没有启用的 Transmission 下载器")
        # 有多个时优先 MP 的默认下载器。
        chosen = next((item for item in options if item.get("default")), options[0])
        return str(chosen["name"])

    async def download(self, media_in: dict[str, Any], torrent_in: dict[str, Any], downloader: str | None = None) -> Any:
        """经 MoviePilot 添加下载：只传 TMDB 编号，由 MoviePilot 自己识别影片。

        必须用 ``/api/v1/download/add``：它按 ``media_source`` + ``media_id`` 完整识别，得到分类（华语电影、
        欧美电影等）并据此选择下载目录。``/api/v1/download/``（含媒体信息）直接采用传入的媒体信息、不再识别，
        没有分类，种子会落在下载目录根下（1.90 之前的问题）。
        """
        tmdb_id = to_int(media_in.get("tmdb_id"))
        if tmdb_id <= 0:
            raise RuntimeError("缺少 TMDB 编号，MoviePilot 无法识别分类")
        torrent_payload = dict(torrent_in)
        factor = torrent_payload.pop("volume_factor", None)
        if factor is not None and torrent_payload.get("downloadvolumefactor") is None:
            torrent_payload["downloadvolumefactor"] = to_float(factor)
        publish_time = torrent_payload.pop("publish_time", None)
        if publish_time and not torrent_payload.get("pubdate"):
            torrent_payload["pubdate"] = str(publish_time)
        torrent_payload.pop("detail_url", None)
        async def add(by_id: bool) -> Any:
            body: dict[str, Any] = {"torrent_in": torrent_payload, "downloader": downloader}
            if by_id:
                body.update({"media_source": "themoviedb", "media_id": str(tmdb_id)})
            async with self._client() as client:
                response = await safe_request(
                    client, "POST", "/api/v1/download/add", headers=self.headers, json=body,
                    label="MoviePilot 地址", allow_private=True,
                )
                response.raise_for_status()
                return response.json()

        title = str(torrent_payload.get("title") or "")
        if await _tmdb_has_tv(tmdb_id):
            # 编号同时是电影和电视剧：MoviePilot 按编号会随便挑一个（曾把电影当成电视剧放进“欧美剧集”目录）。
            # 改按种子名提交，而且只在 MoviePilot 按这个名字识别出的正是这部电影时才提交；种子名是外文拼写、
            # 识别不出时（如 Leon The Professional），把 TMDB 的中文名与原名放到片名前面再试，认不出就不提交，免得放错分类。
            for candidate_title in _recognition_titles(title, media_in):
                media = await self.recognize(candidate_title)
                if media and to_int(media.get("tmdb_id")) == tmdb_id and str(media.get("type")) in {"电影", "movie"}:
                    torrent_payload["title"] = candidate_title
                    return await add(False)
            raise RuntimeError(
                f"TMDB 编号 {tmdb_id} 同时是电影和电视剧，MoviePilot 按编号会当成电视剧，按种子名也没能识别成这部电影，"
                "已暂停提交，请在 MoviePilot 里手动添加",
            )
        result = await add(True)
        if isinstance(result, dict) and result.get("success") is False and "无法识别媒体信息" in str(result.get("message") or ""):
            # 同一个 TMDB 编号同时是电影和电视剧（如 123678、408）时，MoviePilot 只凭编号分不清类型；
            # 种子名本身能被它识别成同一部影片的话，改按种子名识别（分类与下载目录照常）。
            media = await self.recognize(str(torrent_payload.get("title") or ""))
            if media and to_int(media.get("tmdb_id")) == tmdb_id and str(media.get("type")) in {"电影", "movie"}:
                return await add(False)
        return result

# 已查过的“这个 TMDB 编号是否同时是电视剧”（进程内缓存）。
_tmdb_tv_collisions: dict[int, bool] = {}


def _recognition_titles(title: str, media_in: dict[str, Any]) -> list[str]:
    """提交给 MoviePilot 识别的片名候选：先是种子原名，再是“TMDB 中文名 / 原名 + 年份 + 种子名里年份之后的部分”。"""
    candidates = [title]
    year = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", title)
    tail = title[year.end():].strip() if year else title
    release_year = str(media_in.get("tmdb_year") or media_in.get("year") or (year.group(0) if year else "")).strip()
    zh = str(media_in.get("tmdb_title") or "").strip()
    original = str(media_in.get("tmdb_original_title") or "").strip()
    for names in ((zh, original), (original,), (zh,)):
        name = " ".join(part for part in names if part)
        if name and release_year:
            candidate = f"{name} {release_year} {tail}".strip()
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


async def _tmdb_has_tv(tmdb_id: int) -> bool:
    """TMDB 的电影编号与电视剧编号各成一套、会重号（如 10096 既是电影《女孩梦三十》也是电视剧《Go》）。

    查不到（没配 TMDB、网络不通）时按“没重号”处理，不因为 TMDB 抖动挡住提交。"""
    if tmdb_id in _tmdb_tv_collisions:
        return _tmdb_tv_collisions[tmdb_id]
    if not settings.tmdb_api_key:
        return False
    client = TMDBClient()
    try:
        _, params = client._auth()
        await client._get(f"/tv/{int(tmdb_id)}", params)
        found = True
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            return False
        found = False
    except Exception:
        return False
    _tmdb_tv_collisions[tmdb_id] = found
    return found
