"""下载页：Transmission 里种子的实时状态（只读）。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

from ..responses import DownloadsPage
from ..services.downloads import downloads_overview
from ..state import fetch_once

router = APIRouter()

# 下载页每 5 秒刷新、多个标签页也会同时请求：几秒内共用同一份结果，不重复读 Transmission 与 MoviePilot。
OVERVIEW_TTL_SECONDS = 4.0
_overview: tuple[float, dict[str, Any]] | None = None


@router.get("/api/downloads", response_model=DownloadsPage)
async def downloads() -> dict[str, Any]:
    global _overview
    now = time.monotonic()
    if _overview is not None and now - _overview[0] < OVERVIEW_TTL_SECONDS:
        return _overview[1]
    result = await fetch_once("downloads-overview", downloads_overview)
    _overview = (time.monotonic(), result)
    return result
