"""测试兼容层（审计 2-12）。

main.py 保持精简：仅为应用入口（FastAPI 实例、中间件、生命周期）。
单元测试所需的业务符号统一从真实模块导入并聚合于此；测试改用
``from app.compat import main`` 访问，不再经过 app.main 的再导出。
"""

from __future__ import annotations

import types

from .main import app

from .database import cleanup_old_data, connect, initialize  # noqa: F401
from .api.cart import cart, download_cart, history, toggle_cart  # noqa: F401
from .api.playlists import (  # noqa: F401
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
from .api.search import candidates, create_task, task_attempts, task_logs, task_status  # noqa: F401
from .api.system import put_config, validated_base_url  # noqa: F401
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
from .logs import configure_logging  # noqa: F401
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
    utc_now,
    validate_remote_icon_url,
    volume_factor_value,
)
from fastapi import HTTPException  # noqa: F401
from .security import safe_error  # noqa: F401
from .state import (  # noqa: F401
    AUTH_EXEMPT_PATHS,
    MAX_RUNNING_SEARCH_TASKS,
    enforce_search_task_capacity,
    poster_cache,
    raw_candidates,
    require_configured_cookiecloud_uuid,
    running_tasks,
)

main = types.SimpleNamespace(
    app=app,
    utc_now=utc_now,
    cleanup_old_data=cleanup_old_data,
    connect=connect,
    initialize=initialize,
    HTTPException=HTTPException,
    safe_error=safe_error,
    cart=cart,
    download_cart=download_cart,
    history=history,
    toggle_cart=toggle_cart,
    configure_logging=configure_logging,
    configure_playlist_automation=configure_playlist_automation,
    configure_playlist_sync=configure_playlist_sync,
    import_playlist=import_playlist,
    overview=overview,
    playlist_item_poster=playlist_item_poster,
    playlist_items=playlist_items,
    preview_playlist_import=preview_playlist_import,
    refresh_playlist_source=refresh_playlist_source,
    reorder_playlists=reorder_playlists,
    candidates=candidates,
    create_task=create_task,
    task_attempts=task_attempts,
    task_logs=task_logs,
    task_status=task_status,
    put_config=put_config,
    validated_base_url=validated_base_url,
    AIRecognitionClient=AIRecognitionClient,
    EmbyClient=EmbyClient,
    MoviePilotClient=MoviePilotClient,
    MTeamClient=MTeamClient,
    NexusPHPClient=NexusPHPClient,
    RSSClient=RSSClient,
    TMDBClient=TMDBClient,
    TorznabClient=TorznabClient,
    TransmissionClient=TransmissionClient,
    candidate_identity=candidate_identity,
    canonical_item_original_title=canonical_item_original_title,
    canonical_item_title=canonical_item_title,
    canonical_item_year=canonical_item_year,
    ConfigPayload=ConfigPayload,
    ImportPayload=ImportPayload,
    PlaylistAutomationPayload=PlaylistAutomationPayload,
    PlaylistOrderPayload=PlaylistOrderPayload,
    PlaylistSyncPayload=PlaylistSyncPayload,
    TaskPayload=TaskPayload,
    parse_xlsx=parse_xlsx,
    resolve_import=resolve_import,
    library_details=library_details,
    analyze_candidate=analyze_candidate,
    persist_tmdb_item=persist_tmdb_item,
    recognize_movie=recognize_movie,
    build_search_queries=build_search_queries,
    classify_search_error=classify_search_error,
    create_followup_search_task=create_followup_search_task,
    run_search=run_search,
    searchable_playlist_items=searchable_playlist_items,
    resolve_site_adapter=resolve_site_adapter,
    decode_cookiecloud_body=decode_cookiecloud_body,
    raster_image_media_type=raster_image_media_type,
    validate_remote_icon_url=validate_remote_icon_url,
    volume_factor_value=volume_factor_value,
    AUTH_EXEMPT_PATHS=AUTH_EXEMPT_PATHS,
    MAX_RUNNING_SEARCH_TASKS=MAX_RUNNING_SEARCH_TASKS,
    enforce_search_task_capacity=enforce_search_task_capacity,
    poster_cache=poster_cache,
    raw_candidates=raw_candidates,
    require_configured_cookiecloud_uuid=require_configured_cookiecloud_uuid,
    running_tasks=running_tasks,
)
