"""动态页的数据来源：提交记录、寻片 / 识别 / Emby 状态刷新任务与通知，均按时间倒序。"""

from __future__ import annotations

import sqlite3
from typing import Literal


def recent_submissions(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """提交记录，附所属影片（``film_id``）的名称与入馆状态；影片已删除时这些字段为空。"""
    return conn.execute(
        """SELECT h.id,h.created_at,h.success,h.message,h.site_name,h.torrent_name,
                  COALESCE(h.playlist_item_id,c.playlist_item_id) AS film_id,
                  p.tmdb_title,p.chinese_title,p.original_title,p.library_state
           FROM download_history h LEFT JOIN candidates c ON c.id=h.candidate_id
           LEFT JOIN playlist_items p ON p.id=COALESCE(h.playlist_item_id,c.playlist_item_id)
           ORDER BY h.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def recent_search_tasks(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT t.id,t.status,t.total,t.completed,t.matched,t.trigger,t.item_ids_json,t.error_message,
                  t.created_at,t.updated_at,p.name AS playlist_name
           FROM search_tasks t JOIN playlists p ON p.id=t.playlist_id ORDER BY t.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def recent_playlist_tasks(conn: sqlite3.Connection, kind: Literal["recognition", "library"], limit: int) -> list[sqlite3.Row]:
    """识别或 Emby 状态刷新任务；``amount`` 是识别到的或已入馆的影片数，``mode`` 只有识别任务区分。"""
    if kind == "recognition":
        table, columns = "recognition_tasks", "t.matched AS amount,t.mode,t.corrected"
    else:
        table, columns = "library_scan_tasks", "t.in_library AS amount,'missing' AS mode,0 AS corrected"
    return conn.execute(
        f"""SELECT t.id,t.status,t.total,t.completed,{columns},
                   t.error_message,t.created_at,t.updated_at,p.name AS playlist_name
            FROM {table} t JOIN playlists p ON p.id=t.playlist_id ORDER BY t.id DESC LIMIT ?""",  # nosec B608
        (limit,),
    ).fetchall()


def recent_notifications(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id,level,title,message,created_at FROM notifications ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall()
