"""统一同步：立即对齐 Transmission、MoviePilot、Emby 与片单影片的状态，并接收三方主动推送的事件。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..clients import TransmissionClient
from ..responses import SyncEventAccepted, SyncResult, SyncStatus, SyncWebhooks
from ..services import sync, sync_events

router = APIRouter()


@router.post("/api/sync/refresh", response_model=SyncResult)
async def refresh() -> dict[str, Any]:
    return await sync.reconcile()


@router.get("/api/sync/status", response_model=SyncStatus)
async def sync_status() -> dict[str, Any]:
    # 重启后还没同步过时，打开联动页就在后台同步一次，状态随即有内容。
    if sync.status()["last"] is None:
        sync.kick()
    return {**sync.status(), **sync_events.stats()}


async def _webhooks(token: str) -> dict[str, Any]:
    try:
        hooks: dict[str, str] | None = await asyncio.wait_for(TransmissionClient().script_hooks(), timeout=5)
    except Exception:
        hooks = None  # 未配置或读不到 Transmission 时界面不显示钩子状态
    return {"paths": {source: f"/api/sync/events/{source}?token={token}" for source in sync_events.SOURCES}, "transmission_hooks": hooks}


@router.get("/api/sync/webhooks", response_model=SyncWebhooks)
async def sync_webhooks() -> dict[str, Any]:
    """三方推送事件的地址（相对路径，含密钥）与 Transmission 脚本钩子的启用情况，供设置里的“联动”页展示。"""
    return await _webhooks(sync_events.sync_token())


@router.post("/api/sync/token/reset", response_model=SyncWebhooks)
async def reset_sync_token() -> dict[str, Any]:
    """重置事件密钥：旧地址立即失效。"""
    return await _webhooks(sync_events.reset_token())


@router.post("/api/sync/events/{source}", response_model=SyncEventAccepted)
async def sync_event(source: str, request: Request) -> dict[str, Any]:
    """MoviePilot（Webhook 插件）、Transmission（添加 / 完成脚本）、Emby（Webhooks）推送的事件。

    这条路径不走访问令牌，改用地址里的事件密钥校验；事件只作为提示触发同步。"""
    if source not in sync_events.SOURCES:
        raise HTTPException(404, "未知的事件来源")
    client_ip = request.client.host if request.client else "unknown"
    if not sync_events.authorized(request.query_params.get("token"), client_ip):
        raise HTTPException(401, "事件密钥无效")
    query = {key: value for key, value in request.query_params.items() if key != "token"}
    hint = sync_events.parse(source, await request.body(), query)
    if hint is None:
        return {"accepted": False}
    sync_events.handle(hint)
    return {"accepted": True}
