"""馆务 → 备份：数据库备份的列出、创建、恢复与删除。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..logs import event_logger
from ..responses import BackupCreated, BackupRecord, BackupRestored, BackupsDeleted
from ..schemas import BackupDeletePayload
from ..services import backups as backup_service

router = APIRouter()


def _fail(exc: backup_service.BackupError) -> HTTPException:
    return HTTPException(409, str(exc))


@router.get("/api/backups", response_model=list[BackupRecord])
async def list_backups() -> list[dict[str, Any]]:
    return backup_service.list_backups()


@router.post("/api/backups", response_model=BackupCreated)
async def create_backup() -> dict[str, Any]:
    try:
        name = backup_service.create_backup()
    except backup_service.BackupError as exc:
        raise _fail(exc) from exc
    event_logger().info("backup_created", extra={"detail": f"手动备份 {name}"})
    return {"name": name}


@router.post("/api/backups/{name}/restore", response_model=BackupRestored)
async def restore_backup(name: str) -> dict[str, Any]:
    try:
        safety = backup_service.restore_backup(name)
    except backup_service.BackupError as exc:
        raise _fail(exc) from exc
    event_logger().warning("backup_restored", extra={"detail": f"从 {name} 恢复数据库，恢复前备份 {safety}"})
    return {"restored": name, "safety_backup": safety}


@router.post("/api/backups/delete", response_model=BackupsDeleted)
async def delete_backups(payload: BackupDeletePayload) -> dict[str, Any]:
    try:
        deleted = backup_service.delete_backups(payload.names)
    except backup_service.BackupError as exc:
        raise _fail(exc) from exc
    event_logger().info("backups_deleted", extra={"detail": f"删除 {deleted} 份备份"})
    return {"deleted": deleted}
