"""升级搜索（洗版）：已入馆影片当前还留着的升级候选。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..util import rows_to_dicts


def films_with_upgrade_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """最近一次寻片就是升级搜索、且还有合格候选的已入馆影片（新的在前）。"""
    return rows_to_dicts(conn.execute(
        """SELECT p.id,p.rank_no,p.original_title,p.chinese_title,p.tmdb_title,p.year,p.tmdb_year,
                  t.id AS task_id,t.upgrade_above,t.created_at AS searched_at,
                  COUNT(DISTINCT COALESCE(c.resource_key,c.id)) AS candidate_count,
                  SUM(CASE WHEN sel.candidate_id IS NULL THEN 0 ELSE 1 END) AS selected_count,
                  MAX(c.resolution) AS best_resolution
           FROM candidates c
           JOIN search_tasks t ON t.id=c.task_id
           JOIN playlist_items p ON p.id=c.playlist_item_id
           LEFT JOIN selection_items sel ON sel.candidate_id=c.id
           WHERE t.upgrade_above IS NOT NULL AND c.eligibility='eligible' AND p.library_state='in_library'
             AND t.id=(SELECT MAX(c2.task_id) FROM candidates c2 WHERE c2.playlist_item_id=p.id)
           GROUP BY p.id ORDER BY t.id DESC"""
    ).fetchall())
