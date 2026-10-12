"""数据库备份与恢复：手动备份放在数据目录的 backups/，部署前的备份（playlist-autodown.db.bak-*）只读列出。"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..database import connect, initialize
from ..migrations import SCHEMA_VERSION
from ..tasks import active_tasks

MANUAL_KEEP = 10
MANUAL_PATTERN = re.compile(r"^autolist-(?:restore-)?\d{8}-\d{6}\.db$")
DEPLOY_PREFIX = "playlist-autodown.db.bak-"


class BackupError(Exception):
    """备份或恢复无法进行；消息可直接显示给操作者。"""


def _data_dir() -> Path:
    return Path(settings.data_dir)


def _backup_dir() -> Path:
    return _data_dir() / "backups"


def _kind(name: str) -> str | None:
    if MANUAL_PATTERN.match(name):
        return "manual"
    if name.startswith(DEPLOY_PREFIX) and "/" not in name and not name.endswith(("-wal", "-shm")):
        return "deploy"
    return None


def _path(name: str) -> Path:
    kind = _kind(name)
    if kind is None:
        raise BackupError("备份不存在")
    path = (_backup_dir() if kind == "manual" else _data_dir()) / name
    if not path.is_file():
        raise BackupError("备份不存在")
    return path


def _schema_version(path: Path) -> int | None:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        return None


def list_backups() -> list[dict[str, Any]]:
    files = [(path, "manual") for path in _backup_dir().glob("*.db")] if _backup_dir().is_dir() else []
    files += [(path, "deploy") for path in _data_dir().glob(f"{DEPLOY_PREFIX}*")]
    rows = []
    for path, kind in files:
        if _kind(path.name) != kind or not path.is_file():
            continue
        stat = path.stat()
        version = _schema_version(path)
        rows.append({
            "name": path.name,
            "kind": kind,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "restorable": version is not None and version <= SCHEMA_VERSION,
        })
    return sorted(rows, key=lambda row: row["created_at"], reverse=True)


def _snapshot(prefix: str) -> Path:
    """用 SQLite 在线备份接口复制当前数据库（写入中也一致），权限 600。"""
    directory = _backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    moment = datetime.now()
    target = directory / f"{prefix}-{moment.strftime('%Y%m%d-%H%M%S')}.db"
    while target.exists():  # 同一秒内连续备份：文件名顺延一秒
        moment += timedelta(seconds=1)
        target = directory / f"{prefix}-{moment.strftime('%Y%m%d-%H%M%S')}.db"
    with connect() as source:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    target.chmod(0o600)
    manual = sorted(directory.glob("*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in manual[MANUAL_KEEP:]:
        stale.unlink(missing_ok=True)
    return target


def create_backup() -> str:
    return _snapshot("autolist").name


def delete_backups(names: list[str]) -> int:
    """批量删除备份（手动与部署前的都可以）；先全部校验，任何一个不存在就一个也不删。"""
    paths = [_path(name) for name in dict.fromkeys(names)]
    for path in paths:
        path.unlink()
        for suffix in ("-wal", "-shm"):  # 备份被当作数据库打开过时留下的附属文件
            path.with_name(path.name + suffix).unlink(missing_ok=True)
    return len(paths)


def restore_backup(name: str) -> str:
    """把备份内容覆盖回当前数据库；覆盖前先自动备份一份当前数据，返回这份“恢复前”备份的文件名。"""
    path = _path(name)
    with connect() as conn:
        if active_tasks(conn):
            raise BackupError("还有寻片、识别等任务在进行，请等它们结束或取消后再恢复")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("备份文件已损坏，无法恢复")
            version = int(check.execute("PRAGMA user_version").fetchone()[0])
            has_items = check.execute("SELECT 1 FROM sqlite_master WHERE name='playlist_items'").fetchone()
    except sqlite3.Error as exc:
        raise BackupError("备份文件无法读取") from exc
    if not has_items:
        raise BackupError("这不是 AutoList 的数据库备份")
    if version > SCHEMA_VERSION:
        raise BackupError("备份来自更新的版本，当前程序不能恢复")
    safety = _snapshot("autolist-restore")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source, connect() as live:
            source.backup(live)
    except sqlite3.OperationalError as exc:
        raise BackupError(f"数据库正忙，恢复未执行（已有恢复前备份 {safety.name}），稍后再试") from exc
    initialize()
    return safety.name
