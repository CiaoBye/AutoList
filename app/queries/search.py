"""寻片任务、站点尝试记录与任务日志的读写。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..database import json_value
from ..sites.engine import search_page_url
from ..util import json_ids, rows_to_dicts, to_int, utc_now

_ATTEMPT_COUNTS = """COUNT(*) AS total,
    SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed"""


def get_search_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM search_tasks WHERE id=?", (task_id,)).fetchone()


def item_ids_in_rank_range(conn: sqlite3.Connection, playlist_id: int, range_start: int, range_end: int) -> list[int]:
    return [to_int(row["id"]) for row in conn.execute(
        "SELECT id FROM playlist_items WHERE playlist_id=? AND rank_no BETWEEN ? AND ? ORDER BY rank_no",
        (playlist_id, range_start, range_end),
    ).fetchall()]


def item_in_active_search(conn: sqlite3.Connection, playlist_id: int, item_id: int) -> bool:
    """影片是否在排队或进行中的寻片任务里。"""
    return any(
        item_id in (json_ids(row["item_ids_json"]) or [])
        for row in conn.execute(
            "SELECT item_ids_json FROM search_tasks WHERE status IN ('queued','running') AND playlist_id=?", (playlist_id,),
        ).fetchall()
    )


def insert_search_task(
    conn: sqlite3.Connection, playlist_id: int, *, range_start: int, range_end: int, trigger: str,
    site_ids: list[int], item_ids: list[int],
) -> int:
    now = utc_now()
    return to_int(conn.execute(
        """INSERT INTO search_tasks(
             playlist_id,range_start,range_end,status,total,trigger,site_ids_json,item_ids_json,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (playlist_id, range_start, range_end, "queued", len(item_ids), trigger, json_value(site_ids), json_value(item_ids), now, now),
    ).lastrowid)


def active_search_progress(conn: sqlite3.Connection, playlist_id: int | None) -> dict[str, int] | None:
    """排队或进行中的寻片任务合计进度（可按片单），没有时返回 None。"""
    sql = "SELECT id,completed,total FROM search_tasks WHERE status IN ('queued','running')"
    params: tuple[Any, ...] = ()
    if playlist_id is not None:
        sql += " AND playlist_id=?"
        params = (playlist_id,)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    if not rows:
        return None
    return {
        "task_id": to_int(rows[0]["id"]),
        "completed": sum(to_int(row["completed"]) for row in rows),
        "total": sum(to_int(row["total"]) for row in rows),
    }


def recent_search_tasks(conn: sqlite3.Connection, playlist_id: int | None, limit: int) -> list[dict[str, Any]]:
    """最近的寻片任务（不含已归档），每项附 ``attempt_summary``。"""
    if playlist_id is None:
        rows = conn.execute(
            "SELECT * FROM search_tasks WHERE status!='archived' ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM search_tasks WHERE playlist_id=? AND status!='archived' ORDER BY id DESC LIMIT ?",
            (playlist_id, limit),
        ).fetchall()
    tasks = rows_to_dicts(rows)
    if tasks:
        # 一次 IN 查询合并各任务聚合，避免逐任务查询。
        task_ids = [task["id"] for task in tasks]
        placeholders = ",".join("?" for _ in task_ids)
        summaries = {
            row["task_id"]: dict(row)
            for row in conn.execute(
                f"SELECT task_id,{_ATTEMPT_COUNTS} FROM search_attempts WHERE task_id IN ({placeholders}) GROUP BY task_id",  # nosec B608
                task_ids,
            ).fetchall()
        }
        for task in tasks:
            task["attempt_summary"] = summaries.get(task["id"], {"total": 0, "succeeded": 0, "failed": 0})
    return tasks


def attempt_summary(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    return dict(conn.execute(f"SELECT {_ATTEMPT_COUNTS} FROM search_attempts WHERE task_id=?", (task_id,)).fetchone())  # nosec B608


def task_attempts(conn: sqlite3.Connection, task_id: int, limit: int) -> list[dict[str, Any]]:
    """最近的站点尝试（按时间正序）。"""
    rows = conn.execute(
        """SELECT a.id,a.playlist_item_id,p.rank_no,p.original_title,a.site_id,a.site_name,
                  a.attempt_no,a.status,a.result_count,a.duration_ms,a.error_code,a.error_message,a.finished_at
           FROM search_attempts a JOIN playlist_items p ON p.id=a.playlist_item_id
           WHERE a.task_id=? ORDER BY a.id DESC LIMIT ?""", (task_id, limit),
    ).fetchall()
    return list(reversed(rows_to_dicts(rows)))


def task_site_summaries(conn: sqlite3.Connection, task_id: int) -> list[dict[str, Any]]:
    """各站点的成功 / 失败次数与平均耗时；遇到搜索人机验证的站点附上种子搜索页地址，供用户去验证。"""
    rows = rows_to_dicts(conn.execute(
        f"""SELECT a.site_id,a.site_name,{_ATTEMPT_COUNTS},CAST(AVG(a.duration_ms) AS INTEGER) AS average_ms,
                   MAX(a.error_code='site_captcha') AS needs_captcha,s.base_url
            FROM search_attempts a LEFT JOIN pt_sites s ON s.id=a.site_id
            WHERE a.task_id=? GROUP BY a.site_id,a.site_name ORDER BY a.site_name""",  # nosec B608
        (task_id,),
    ).fetchall())
    for row in rows:
        base_url = row.pop("base_url", None)
        row["verify_url"] = search_page_url(base_url) if row.pop("needs_captcha", 0) and base_url else None
    return rows


def task_logs(conn: sqlite3.Connection, task_id: int, limit: int) -> list[dict[str, Any]]:
    """最近的任务日志（按时间正序）。"""
    rows = conn.execute(
        "SELECT id,level,stage,message,created_at FROM search_task_logs WHERE task_id=? ORDER BY id DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    return list(reversed(rows_to_dicts(rows)))
