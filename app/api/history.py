"""提交记录：查看与按状态清理。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..services.history import clear_download_history, projected_download_history
from ..responses import HistoryCleared, HistoryRecord

router = APIRouter()


@router.get("/api/history", response_model=list[HistoryRecord])
async def history() -> list[dict[str, Any]]:
    return await projected_download_history()


@router.delete("/api/history", response_model=HistoryCleared)
async def delete_history(status: str = "all") -> dict[str, Any]:
    try:
        deleted = await clear_download_history(status)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"deleted": deleted, "status": status}
