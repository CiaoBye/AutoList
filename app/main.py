"""AutoList FastAPI entrypoint: middleware, routers, and compatibility re-exports."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
import re
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import state
from .api import cart as cart_routes
from .api import films as film_routes
from .api import logs as log_routes
from .api import playlists as playlist_routes
from .api import search as search_routes
from .api import sites as site_routes
from .api import system as system_routes
from .config import (
    APP_VERSION,
    access_token,
    access_token_is_strong,
    access_token_required,
    access_token_strength_enforced,
    access_token_validation_error,
    load_runtime_settings,
)
from .database import cleanup_old_data, connect, initialize
from .logs import configure_logging, event_logger
from .security import extract_access_token, is_signed_media_path, media_signature_matches, sanitize_sensitive_text, token_matches
from .schemas import MAX_IMPORT_JSON_BYTES
from .state import AUTH_EXEMPT_PATHS
from .services.automation import sync_scheduler
from .clients import close_search_clients
from .util import utc_now
from starlette.exceptions import HTTPException as StarletteHTTPException

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize()
    load_runtime_settings()
    configure_logging()
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
        await close_search_clients()


_ENABLE_DOCS = os.getenv("AUTOLIST_ENABLE_DOCS", "").strip().lower() == "true"

app = FastAPI(
    title="AutoList",
    version=APP_VERSION,
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
app.include_router(film_routes.router)
app.include_router(log_routes.router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, str):
        detail = sanitize_sensitive_text(detail, 500)
    return JSONResponse(
        {"detail": detail},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    event_logger().error(f"unhandled_server_error: {request.method} {request.url.path}", exc_info=exc)
    return JSONResponse(
        {"detail": "系统内部错误，请稍后重试"},
        status_code=500,
    )

# 非 CookieCloud 普通端点请求体上限（CookieCloud 由 read_request_body_limited
# 单独限 40MB）。片单导入的字段上限来自 ImportPayload：xlsx_base64 最多
# 50,000,000 个 ASCII 字符，csv_text 最多 20,000,000 个 Unicode 字符。
# JSON 字符串在 HTTP 层可能使用 \uXXXX 转义；按 UTF-16 surrogate pair 的
# 最坏情况预留 12 字节/字符，再加 JSON 包装开销，确保 HTTP 限制不会比
# Schema 更窄。这个较大的上限只对两个导入路由生效。
MAX_REQUEST_BODY_BYTES = 5 * 1024 * 1024
IMPORT_XLSX_BASE64_MAX_CHARS = 50_000_000
IMPORT_CSV_MAX_CHARS = 20_000_000
IMPORT_JSON_ESCAPE_MAX_BYTES_PER_CHAR = 12
IMPORT_JSON_ENVELOPE_BYTES = 4096
MAX_IMPORT_HTTP_BODY_BYTES = max(
    IMPORT_XLSX_BASE64_MAX_CHARS + IMPORT_JSON_ENVELOPE_BYTES,
    IMPORT_CSV_MAX_CHARS * IMPORT_JSON_ESCAPE_MAX_BYTES_PER_CHAR + IMPORT_JSON_ENVELOPE_BYTES,
    MAX_IMPORT_JSON_BYTES * 3 + IMPORT_JSON_ENVELOPE_BYTES,
)
IMPORT_ROUTE_PATHS = frozenset({"/api/playlists/import", "/api/playlists/import/preview"})
TOKEN_FAILURE_WINDOW_SECONDS = 60
TOKEN_FAILURE_LIMIT = 10
_MAX_TRACKED_FAILURE_IPS = 1000
_token_failure_by_ip: dict[str, list[float]] = {}


def _token_failure_rate_limited(client_ip: str = "unknown") -> bool:
    """按客户端 IP 限制令牌失败尝试频率，防止单个攻击者枚举或造成全局 DoS。"""
    import time

    now = time.monotonic()
    if len(_token_failure_by_ip) > _MAX_TRACKED_FAILURE_IPS:
        stale_ips = [
            ip for ip, timestamps in _token_failure_by_ip.items()
            if not timestamps or now - timestamps[-1] > TOKEN_FAILURE_WINDOW_SECONDS
        ]
        for ip in stale_ips:
            _token_failure_by_ip.pop(ip, None)

    failures = _token_failure_by_ip.setdefault(client_ip, [])
    while failures and now - failures[0] > TOKEN_FAILURE_WINDOW_SECONDS:
        failures.pop(0)
    if len(failures) >= TOKEN_FAILURE_LIMIT:
        return True
    failures.append(now)
    return False


def is_cookiecloud_request_path(raw_path: str) -> bool:
    path = re.sub(r"/+", "/", raw_path).rstrip("/") or "/"
    return (
        path == "/cookiecloud"
        or path.startswith("/cookiecloud/")
        or path == "/update"
        or path.startswith("/update/")
        or path.startswith("/get/")
        or path.startswith("/cookiecloud/get/")
    )


@app.middleware("http")
async def cookiecloud_cors_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """支持 Chrome CookieCloud 扩展跨域同步与 Private Network Access (PNA)。"""
    if is_cookiecloud_request_path(request.url.path):
        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Allow-Private-Network": "true",
                    "Access-Control-Max-Age": "86400",
                },
            )
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response
    return await call_next(request)


@app.middleware("http")
async def request_size_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """拒绝声明过大的请求体，避免未认证请求耗尽内存（审计 3-14）。"""
    if not is_cookiecloud_request_path(request.url.path):
        body_limit = MAX_IMPORT_HTTP_BODY_BYTES if request.url.path in IMPORT_ROUTE_PATHS else MAX_REQUEST_BODY_BYTES
        content_length = request.headers.get("content-length")
        if content_length is None:
            # chunked/无长度请求绕过 Content-Length 检查（审计 POST-006）：
            # 非 GET/HEAD 且带 body 的请求必须声明长度，避免 Starlette 全量缓冲。
            if request.method not in {"GET", "HEAD", "DELETE", "OPTIONS"}:
                return JSONResponse({"detail": "请求必须声明 Content-Length"}, status_code=411)
        else:
            try:
                declared_length = int(content_length)
                if declared_length < 0:
                    return JSONResponse({"detail": "请求体长度无效"}, status_code=400)
                if declared_length > body_limit:
                    return JSONResponse({"detail": "请求体过大"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "请求体长度无效"}, status_code=400)
    return await call_next(request)


@app.middleware("http")
async def access_token_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    if not access_token_required():
        return await call_next(request)
    path = request.url.path
    # 健康检查、静态入口和 CookieCloud 保持既有公开契约；所有受保护
    # 路径必须先通过强令牌检查，再考虑签名媒体豁免。
    if is_cookiecloud_request_path(path) or path in AUTH_EXEMPT_PATHS or path.startswith("/assets/"):
        return await call_next(request)
    if access_token_strength_enforced() and not access_token_is_strong():
        # 只返回统一提示，不泄露 configured/validation 等鉴权配置细节（审计 3-12）。
        return JSONResponse(
            {"detail": "服务端访问令牌强度不足，请更换至少 32 个字符的随机令牌"},
            status_code=503,
        )
    signed_media = (
        is_signed_media_path(path)
        and media_signature_matches(
            path, request.query_params.get("expires"), request.query_params.get("signature"),
        )
    )
    if signed_media:
        return await call_next(request)
    provided = extract_access_token(request.headers.get("authorization"), request.headers.get("x-autolist-token"))
    if not token_matches(provided, access_token()):
        client_ip = request.client.host if request.client else "unknown"
        if _token_failure_rate_limited(client_ip):
            return JSONResponse({"detail": "尝试过于频繁，请稍后再试"}, status_code=429)
        return JSONResponse({"detail": "需要有效的访问令牌"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Defense-in-depth headers; the frontend inlines style attributes so
    style-src needs 'unsafe-inline', but scripts stay 'self'-only.
    """
    response = await call_next(request)
    if request.url.path.startswith("/assets/"):
        # app.js 以 ES 模块导入 ./js/core.js，模块地址不带版本号；没有显式缓存策略时
        # 浏览器会按启发式规则继续使用旧模块，升级后新旧脚本混用甚至导入失败。
        # no-cache 仍可缓存，只是每次用 ETag 确认，未变化时服务端返回 304。
        response.headers.setdefault("Cache-Control", "no-cache")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
    )
    return response
