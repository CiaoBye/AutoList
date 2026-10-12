"""站况页：各站点的 Cookie 健康、近期搜索与提交统计、做种情况。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..database import connect
from ..responses import SiteStatusPage
from ..security import signed_media_url
from ..services.site_health import build_site_status

router = APIRouter()


@router.get("/api/site-status", response_model=SiteStatusPage)
async def site_status() -> dict[str, Any]:
    with connect() as conn:
        page = await build_site_status(conn)
    for row in page["sites"]:
        endpoint = signed_media_url(f"/api/sites/{row['id']}/icon")
        row["icon_endpoint"] = f"{endpoint}{'&' if '?' in endpoint else '?'}v=2"
    return page
