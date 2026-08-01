"""AutoList FastAPI entrypoint: middleware, routers, and compatibility re-exports."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import state
from .api import cart as cart_routes
from .api import playlists as playlist_routes
from .api import search as search_routes
from .api import sites as site_routes
from .api import system as system_routes
from .config import access_token, access_token_required, load_runtime_settings
from .database import cleanup_old_data, connect, initialize
from .security import extract_access_token, token_matches
from .services.automation import sync_scheduler
from .state import (  # noqa: F401
    AUTH_EXEMPT_PATHS,
    MAX_RUNNING_SEARCH_TASKS,
    enforce_search_task_capacity,
    poster_cache,
    raw_candidates,
    require_configured_cookiecloud_uuid,
    running_tasks,
)
from .util import utc_now

# Domain / service re-exports for unit tests (`from app import main`).
from fastapi import HTTPException  # noqa: F401

from .clients import (  # noqa: F401
    AIRecognitionClient,
    EmbyClient,
    MoviePilotClient,
    MTeamClient,
    NexusPHPClient,
    RSSClient,
    TMDBClient,
    TorznabClient,
    TransmissionClient,
)
from .domain.titles import (  # noqa: F401
    candidate_identity,
    canonical_item_original_title,
    canonical_item_title,
    canonical_item_year,
)
from .schemas import (  # noqa: F401
    ConfigPayload,
    ImportPayload,
    PlaylistAutomationPayload,
    PlaylistOrderPayload,
    PlaylistSyncPayload,
    TaskPayload,
)
from .services.imports import parse_xlsx, resolve_import  # noqa: F401
from .services.library import library_details  # noqa: F401
from .services.recognition import analyze_candidate, persist_tmdb_item, recognize_movie  # noqa: F401
from .services.search import (  # noqa: F401
    build_search_queries,
    classify_search_error,
    create_followup_search_task,
    run_search,
    searchable_playlist_items,
)
from .services.sites import resolve_site_adapter  # noqa: F401
from .util import (  # noqa: F401
    decode_cookiecloud_body,
    raster_image_media_type,
    validate_remote_icon_url,
    volume_factor_value,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize()
    load_runtime_settings()
    cleanup_old_data()
    with connect() as conn:
        now = utc_now()
        for table in ("search_tasks", "recognition_tasks", "library_scan_tasks"):
            conn.execute(
                f"UPDATE {table} SET status='interrupted', updated_at=? WHERE status IN ('queued','running')",  # nosec B608
                (now,),
            )
        # 自动化排队任务（queued）重启后保留，由调度器在容量释放后继续消费。
        conn.execute(
            "UPDATE automation_runs SET status='interrupted', updated_at=? WHERE status='running'",
            (now,),
        )
    state.scheduler_task = asyncio.create_task(sync_scheduler())
    try:
        yield
    finally:
        if state.scheduler_task:
            state.scheduler_task.cancel()
            await asyncio.gather(state.scheduler_task, return_exceptions=True)


_ENABLE_DOCS = os.getenv("AUTOLIST_ENABLE_DOCS", "").strip().lower() == "true"

app = FastAPI(
    title="AutoList",
    version="0.88",
    lifespan=lifespan,
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url="/redoc" if _ENABLE_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
)
app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "static"), name="assets")
system_routes.APP_VERSION = app.version

app.include_router(system_routes.router)
app.include_router(site_routes.router)
app.include_router(playlist_routes.router)
app.include_router(search_routes.router)
app.include_router(cart_routes.router)


@app.middleware("http")
async def access_token_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    if not access_token_required():
        return await call_next(request)
    path = request.url.path
    if path in AUTH_EXEMPT_PATHS or path.startswith("/assets/") or path.startswith("/cookiecloud/"):
        return await call_next(request)
    provided = extract_access_token(request.headers.get("authorization"), request.headers.get("x-autolist-token"))
    if not token_matches(provided, access_token()):
        return JSONResponse({"detail": "需要有效的访问令牌"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Defense-in-depth headers; the frontend inlines style attributes so
    style-src needs 'unsafe-inline', but scripts stay 'self'-only.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
    )
    return response


# Route handler re-exports used by tests.
from .api.cart import cart, download_cart, history, toggle_cart  # noqa: E402,F401
from .api.playlists import (  # noqa: E402,F401
    configure_playlist_automation,
    configure_playlist_sync,
    import_playlist,
    overview,
    playlist_item_poster,
    playlist_items,
    preview_playlist_import,
    refresh_playlist_source,
    reorder_playlists,
)
from .api.search import (  # noqa: E402,F401
    candidates,
    create_task,
    task_attempts,
    task_logs,
    task_status,
)
from .api.system import put_config, validated_base_url  # noqa: E402,F401
