"""数据库备份与恢复：在线备份、恢复前自动备份、任务进行中拒绝恢复、只允许删除手动备份。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.api import backups as backup_routes
from app.config import settings
from app.database import connect
from app.schemas import BackupDeletePayload
from app.services import backups as backup_service
from app.util import utc_now
from fastapi import HTTPException
from tests.support import SeededPlaylistTestCase


class BackupTests(SeededPlaylistTestCase):
    def _playlist_names(self) -> list[str]:
        with connect() as conn:
            return [row[0] for row in conn.execute("SELECT name FROM playlists ORDER BY id").fetchall()]

    async def test_create_list_and_restore_roundtrip(self) -> None:
        created = await backup_routes.create_backup()
        name = created["name"]
        self.assertEqual(oct((Path(settings.data_dir) / "backups" / name).stat().st_mode & 0o777), "0o600")
        before = self._playlist_names()
        with connect() as conn:
            conn.execute("INSERT INTO playlists(name,created_at) VALUES('备份之后新增',?)", (utc_now(),))
        self.assertEqual(len(self._playlist_names()), len(before) + 1)

        listed = await backup_routes.list_backups()
        self.assertEqual([row["name"] for row in listed], [name])
        self.assertTrue(listed[0]["restorable"])

        result = await backup_routes.restore_backup(name)
        self.assertEqual(self._playlist_names(), before)
        # 恢复前自动备份了“备份之后”的状态，可以再恢复回来。
        self.assertIn(result["safety_backup"], [row["name"] for row in await backup_routes.list_backups()])
        await backup_routes.restore_backup(result["safety_backup"])
        self.assertEqual(len(self._playlist_names()), len(before) + 1)

    async def test_restore_refused_while_tasks_are_active(self) -> None:
        name = (await backup_routes.create_backup())["name"]
        with connect() as conn:
            conn.execute(
                "INSERT INTO library_scan_tasks(playlist_id,status,total,created_at,updated_at) VALUES(?,'running',1,?,?)",
                (self.playlist_id, utc_now(), utc_now()),
            )
        with self.assertRaises(HTTPException) as caught:
            await backup_routes.restore_backup(name)
        self.assertEqual(caught.exception.status_code, 409)

    async def test_rejects_unknown_corrupt_and_foreign_files(self) -> None:
        data = Path(settings.data_dir)
        for bad in ("../playlist-autodown.db", "unknown.db", "playlist-autodown.db.bak-x/../y"):
            with self.subTest(name=bad), self.assertRaises(backup_service.BackupError):
                backup_service.restore_backup(bad)
        (data / "playlist-autodown.db.bak-2.1-20260101").write_bytes(b"not a database")
        with self.assertRaises(backup_service.BackupError):
            backup_service.restore_backup("playlist-autodown.db.bak-2.1-20260101")
        other = data / "playlist-autodown.db.bak-2.2-20260101"
        with sqlite3.connect(other) as conn:
            conn.execute("CREATE TABLE unrelated(id INTEGER)")
        with self.assertRaises(backup_service.BackupError):
            backup_service.restore_backup(other.name)

    async def test_deploy_backups_listed_and_deletable_in_batch(self) -> None:
        data = Path(settings.data_dir)
        names = []
        for version in ("2.79", "2.80"):
            deploy = data / f"playlist-autodown.db.bak-{version}-20260101"
            with connect() as source, sqlite3.connect(deploy) as destination:
                source.backup(destination)
            names.append(deploy.name)
        (data / f"{names[0]}-shm").write_bytes(b"x")  # SQLite 附属文件不算备份
        rows = await backup_routes.list_backups()
        self.assertEqual({(row["name"], row["kind"]) for row in rows}, {(name, "deploy") for name in names})
        # 其中一个不存在：一个也不删。
        with self.assertRaises(HTTPException):
            await backup_routes.delete_backups(BackupDeletePayload(names=[names[0], "autolist-20200101-000000.db"]))
        self.assertTrue(all((data / name).exists() for name in names))
        result = await backup_routes.delete_backups(BackupDeletePayload(names=names))
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(await backup_routes.list_backups(), [])

    async def test_manual_backups_are_pruned_and_deletable(self) -> None:
        directory = Path(settings.data_dir) / "backups"
        directory.mkdir()
        for index in range(backup_service.MANUAL_KEEP + 2):
            (directory / f"autolist-20250101-0000{index:02d}.db").write_bytes(b"x")
        name = (await backup_routes.create_backup())["name"]
        self.assertEqual(len(list(directory.glob("*.db"))), backup_service.MANUAL_KEEP)
        self.assertTrue((directory / name).exists())
        await backup_routes.delete_backups(BackupDeletePayload(names=[name]))
        self.assertFalse((directory / name).exists())
