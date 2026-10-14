"""站点搜索结果的统一结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Torrent:
    title: str
    detail_url: str
    download_url: str
    description: str = ""
    size: int = 0
    seeders: int = 0
    leechers: int = 0
    grabs: int = 0
    publish_time: str | None = None
    download_factor: float = 1.0
    upload_factor: float = 1.0
    imdb_id: str | None = None
    tmdb_id: int | None = None
    # 下载地址来自表单（如某站点的 POST 下载按钮）时为 "post"。
    download_method: str = "get"
    labels: list[str] = field(default_factory=list)
    # 促销（免费 / 折扣）的到期时间，站点页面上的原样文字（如 “2026-10-01 18:12:31”）；没有时限或读不到为空。
    promotion_until: str | None = None

    def as_candidate(self, site: dict[str, Any], user_agent: str) -> dict[str, Any]:
        """寻片流程与 MoviePilot 提交使用的字段（MoviePilot 的 TorrentInfo 字段名一并给出）。"""
        return {
            "title": self.title,
            "description": self.description,
            "site_name": site["name"],
            "size": self.size,
            "seeders": self.seeders,
            "peers": self.leechers,
            "grabs": self.grabs,
            "enclosure": self.download_url,
            "detail_url": self.detail_url,
            "labels": list(self.labels),
            "volume_factor": self.download_factor,
            "uploadvolumefactor": self.upload_factor,
            "publish_time": self.publish_time,
            "promotion_until": self.promotion_until,
            "imdbid": self.imdb_id,
            "tmdbid": self.tmdb_id,
            "download_method": self.download_method,
            "site_cookie": site.get("cookie") or "",
            "site_ua": user_agent,
        }
