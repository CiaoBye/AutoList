"""升级搜索（洗版）：已入馆影片当前留着的升级候选，以及最近已经搜过升级的影片。"""

from __future__ import annotations

import sqlite3

from ..util import json_ids

RECENT_DAYS = 30  # 升级搜索过（无论有没有找到）的影片，这么多天内不再标“可升级”


def ready_item_ids(conn: sqlite3.Connection, item_ids: list[int]) -> set[int]:
    """当前候选来自升级搜索、且还有合格候选的已入馆影片。"""
    if not item_ids:
        return set()
    marks = ",".join("?" for _ in item_ids)
    return {
        int(row[0]) for row in conn.execute(
            f"""SELECT DISTINCT p.id FROM candidates c
                JOIN search_tasks t ON t.id=c.task_id
                JOIN playlist_items p ON p.id=c.playlist_item_id
                WHERE t.upgrade_above IS NOT NULL AND c.eligibility='eligible' AND p.library_state='in_library'
                  AND p.id IN ({marks})
                  AND t.id=(SELECT MAX(c2.task_id) FROM candidates c2 WHERE c2.playlist_item_id=p.id)""",  # nosec B608
            item_ids,
        ).fetchall()
    }


def recently_searched_ids(conn: sqlite3.Connection) -> set[int]:
    """最近 RECENT_DAYS 天内做过升级搜索的影片。"""
    found: set[int] = set()
    for row in conn.execute(
        "SELECT item_ids_json FROM search_tasks WHERE trigger='upgrade' AND datetime(created_at) >= datetime('now', ?)",
        (f"-{RECENT_DAYS} days",),
    ).fetchall():
        found.update(json_ids(row["item_ids_json"]) or [])
    return found
