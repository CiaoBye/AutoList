"""后台任务的统一框架：寻片、识别、Emby 状态刷新与新片处理。

四类任务各自保存在一张表里（寻片任务还挂着候选、尝试记录与日志），但运行登记、容量限制、
取消、服务重启后的恢复和“进行中”列表只在这里实现一次；各类任务只声明表名、名称与容量，
执行函数由各自的服务模块提供。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from .util import utc_now

ACTIVE_STATUSES = ("queued", "running")


@dataclass(eq=False)
class TaskKind:
    key: str
    table: str
    label: str
    limit: int
    # 服务重启后改为“中断”的状态。新片处理的排队任务保留，由调度器在容量释放后继续执行。
    interrupt_on_restart: tuple[str, ...] = ACTIVE_STATUSES
    # 本进程里正在执行的任务；执行结束后自动移除。
    running: dict[int, asyncio.Task[None]] = field(default_factory=dict)

    def active_count(self) -> int:
        return sum(1 for task in self.running.values() if task and not task.done())

    def capacity_error(self, active: int | None = None) -> str | None:
        """同类任务已达上限时返回提示；``active`` 可传入按数据库统计的数量（寻片跨进程计数）。"""
        count = self.active_count() if active is None else active
        if count >= self.limit:
            return f"已有 {count} 个{self.label}任务在运行，请稍后再试"
        return None

    def start(self, task_id: int, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(work)
        self.running[task_id] = task
        task.add_done_callback(lambda finished, key=task_id: self._forget(key, finished))
        return task

    def _forget(self, task_id: int, task: asyncio.Task[None]) -> None:
        if self.running.get(task_id) is task:
            del self.running[task_id]

    def is_running(self, task_id: int) -> bool:
        task = self.running.get(task_id)
        return bool(task and not task.done())

    async def cancel(self, task_id: int) -> bool:
        """取消本进程里正在执行的任务并等待它收尾；任务不在本进程时返回 False。"""
        task = self.running.get(task_id)
        if not task or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True


SEARCH = TaskKind("search", "search_tasks", "寻片", 3)
RECOGNITION = TaskKind("recognition", "recognition_tasks", "识别", 2)
LIBRARY = TaskKind("library", "library_scan_tasks", "Emby 状态刷新", 2)
AUTOMATION = TaskKind("automation", "automation_runs", "新片处理", 2, interrupt_on_restart=("running",))
KINDS: tuple[TaskKind, ...] = (SEARCH, RECOGNITION, LIBRARY, AUTOMATION)


def recover_after_restart(conn: sqlite3.Connection) -> None:
    """服务重启后，上一进程留下的“排队 / 运行中”任务已不会继续执行，统一标记为中断。"""
    now = utc_now()
    for kind in KINDS:
        marks = ",".join("?" for _ in kind.interrupt_on_restart)
        conn.execute(
            f"UPDATE {kind.table} SET status='interrupted', updated_at=? WHERE status IN ({marks})",  # nosec B608
            (now, *kind.interrupt_on_restart),
        )


def active_playlist_task(conn: sqlite3.Connection, playlist_id: int) -> TaskKind | None:
    """片单上有排队或运行中的任务时返回它的类别。"""
    for kind in KINDS:
        if conn.execute(
            f"SELECT 1 FROM {kind.table} WHERE playlist_id=? AND status IN ('queued','running') LIMIT 1",  # nosec B608
            (playlist_id,),
        ).fetchone():
            return kind
    return None


async def cancel_playlist_tasks(conn: sqlite3.Connection, playlist_id: int) -> None:
    """删除片单前取消它所有进行中的任务，并等待任务收尾。"""
    pending: list[asyncio.Task[None]] = []
    for kind in KINDS:
        for row in conn.execute(
            f"SELECT id FROM {kind.table} WHERE playlist_id=? AND status IN ('queued','running')",  # nosec B608
            (playlist_id,),
        ).fetchall():
            task = kind.running.get(int(row["id"]))
            if task and not task.done():
                task.cancel()
                pending.append(task)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def active_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """首页“进行中”：四类任务按同一格式列出。"""
    extra = {"search": "t.trigger", "automation": "t.stage"}
    tasks: list[dict[str, Any]] = []
    for kind in KINDS:
        column = f",{extra[kind.key]}" if kind.key in extra else ""
        for row in conn.execute(
            f"""SELECT t.id,t.status,t.total,t.completed{column},t.playlist_id,p.name AS playlist_name
                FROM {kind.table} t JOIN playlists p ON p.id=t.playlist_id
                WHERE t.status IN ('queued','running') ORDER BY t.id"""  # nosec B608
        ).fetchall():
            tasks.append({"kind": kind.key, **dict(row)})
    return tasks
