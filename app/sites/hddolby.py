"""高清杜比官方搜索接口（参考 MoviePilot 的 HddolbySpider）：不经过网页，因此不受二次验证影响。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .errors import ApiError
from .models import Torrent

MOVIE_CATEGORIES = [401, 405]
PAGE_SIZE = 100
# promotion_time_type → 下载折扣 / 上传倍数（与 MoviePilot 相同）。
DOWNLOAD_FACTORS = {2: 0.0, 5: 0.5, 6: 1.0, 7: 0.3}
UPLOAD_FACTORS = {3: 2.0, 4: 2.0, 6: 2.0}


def search_url(base_url: str) -> str:
    host = (urlparse(base_url).hostname or "").removeprefix("www.")
    return f"https://api.{host}/api/v1/torrent/search"


def request_body(title: str, imdb_id: str | None) -> dict[str, Any]:
    return {"keyword": imdb_id or title, "page_number": 0, "page_size": PAGE_SIZE,
            "categories": MOVIE_CATEGORIES, "visible": 1}


def parse_results(payload: Any, base_url: str) -> list[Torrent]:
    if not isinstance(payload, dict):
        raise ApiError("高清杜比接口返回格式无效")
    if payload.get("error"):
        message = (payload.get("error") or {}).get("message") if isinstance(payload.get("error"), dict) else payload.get("error")
        raise ApiError(f"高清杜比接口返回错误：{message or '未知错误'}")
    base = base_url.rstrip("/") + "/"
    torrents = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        promotion = int(item.get("promotion_time_type") or 0)
        down = DOWNLOAD_FACTORS.get(promotion, 1.0)
        up = UPLOAD_FACTORS.get(promotion, 1.0)
        labels = (["FREE"] if down == 0 else [f"{int(down * 100)}%"] if down < 1 else []) + ([f"{up:g}X"] if up > 1 else [])
        torrents.append(Torrent(
            title=str(item.get("name") or ""),
            description=str(item.get("small_descr") or ""),
            detail_url=f"{base}details.php?id={item['id']}&hit=1",
            download_url=f"{base}download.php?id={item['id']}&downhash={item.get('downhash') or ''}",
            size=int(item.get("size") or 0),
            seeders=int(item.get("seeders") or 0),
            leechers=int(item.get("leechers") or 0),
            grabs=int(item.get("times_completed") or 0),
            publish_time=str(item.get("added") or "")[:10] or None,
            download_factor=down,
            upload_factor=up,
            imdb_id=str(item.get("imdb_id") or "") or None,
            tmdb_id=int(item["tmdb_id"]) if str(item.get("tmdb_id") or "").isdigit() else None,
            labels=labels,
        ))
    return [torrent for torrent in torrents if torrent.title]
