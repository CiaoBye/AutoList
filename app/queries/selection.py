"""待入馆清单与提交记录的读写。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..util import rows_to_dicts, utc_now


def selection_candidate(conn: sqlite3.Connection, candidate_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT eligibility,exclusion_reason,playlist_item_id,site_name,resource_key FROM candidates WHERE id=?",
        (candidate_id,),
    ).fetchone()


def is_selected(conn: sqlite3.Connection, candidate_id: str) -> bool:
    return conn.execute("SELECT 1 FROM selection_items WHERE candidate_id=?", (candidate_id,)).fetchone() is not None


def add_to_selection(conn: sqlite3.Connection, candidate_id: str) -> None:
    conn.execute("INSERT INTO selection_items(candidate_id,selected_at) VALUES(?,?)", (candidate_id, utc_now()))


def remove_from_selection(conn: sqlite3.Connection, candidate_id: str) -> None:
    conn.execute("DELETE FROM selection_items WHERE candidate_id=?", (candidate_id,))


def same_release_selected(conn: sqlite3.Connection, playlist_item_id: int, site_name: str | None, resource_key: str | None) -> bool:
    """同一影片、同一站点、同一发布是否已在清单中。"""
    return conn.execute(
        """SELECT 1 FROM selection_items sel JOIN candidates c ON c.id=sel.candidate_id
           WHERE c.playlist_item_id=? AND c.site_name=? AND COALESCE(c.resource_key,'')=? LIMIT 1""",
        (playlist_item_id, site_name, resource_key or ""),
    ).fetchone() is not None


def selection_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows_to_dicts(conn.execute(
        """SELECT c.id, c.title, c.site_name, c.size, c.resolution, p.library_state, c.detail_url,
                  p.original_title, p.rank_no, p.chinese_title, p.tmdb_title, p.tmdb_original_title, p.tmdb_year, p.year,
                  p.id AS playlist_item_id, p.imdb_id, p.tmdb_id, p.tmdb_imdb_id
           FROM selection_items sel JOIN candidates c ON c.id=sel.candidate_id
           JOIN playlist_items p ON p.id=c.playlist_item_id ORDER BY sel.selected_at"""
    ).fetchall())


def selection_for_submission(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """清单中的候选，连同所属影片当前的身份与入馆状态（字段加 ``playlist_`` 前缀）。"""
    return conn.execute(
        """SELECT c.*, p.original_title AS playlist_original_title,p.chinese_title AS playlist_chinese_title,
                  p.year AS playlist_year,p.imdb_id AS playlist_imdb_id,p.tmdb_id AS playlist_tmdb_id,
                  p.tmdb_title AS playlist_tmdb_title,p.tmdb_original_title AS playlist_tmdb_original_title,
                  p.tmdb_year AS playlist_tmdb_year,p.tmdb_imdb_id AS playlist_tmdb_imdb_id,
                  p.library_state AS playlist_library_state,p.library_checked_at AS playlist_library_checked_at,
                  p.id AS playlist_snapshot_id
           FROM selection_items sel JOIN candidates c ON c.id=sel.candidate_id
           JOIN playlist_items p ON p.id=c.playlist_item_id ORDER BY sel.selected_at"""
    ).fetchall()


def release_already_submitted(
    conn: sqlite3.Connection, candidate_id: str, resource_key: str, playlist_item_id: int, site_name: str | None,
) -> bool:
    """相同发布是否已成功提交过：同候选、同资源指纹，或同影片 + 站点 + 资源指纹。

    同时看 ``candidates.submitted_at``：清空提交记录不解除防重复。
    """
    return conn.execute(
        """SELECT 1 FROM download_history h
           WHERE h.success=1 AND (
             h.candidate_id=? OR h.resource_key=? OR EXISTS (
               SELECT 1 FROM candidates c WHERE c.id=h.candidate_id
                 AND c.playlist_item_id=? AND c.site_name=? AND c.resource_key=?
             )
           )
           UNION ALL
           SELECT 1 FROM candidates c
           WHERE c.id=? AND c.submitted_at IS NOT NULL
           LIMIT 1""",
        (candidate_id, resource_key, playlist_item_id, site_name, resource_key, candidate_id),
    ).fetchone() is not None


def failure_recorded(conn: sqlite3.Connection, candidate_id: str, message: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM download_history WHERE candidate_id=? AND success=0 AND message=? LIMIT 1",
        (candidate_id, message),
    ).fetchone() is not None


def record_submission(
    conn: sqlite3.Connection, candidate: sqlite3.Row, *, snapshot_json: str, resource_key: str,
    success: bool, message: str | None, submission_hash: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO download_history(
               candidate_id,playlist_item_id,playlist_item_snapshot_json,resource_key,title,torrent_name,site_name,
               submission_hash,success,message,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (candidate["id"], candidate["playlist_item_id"], snapshot_json, resource_key, candidate["playlist_original_title"],
         candidate["title"], candidate["site_name"], submission_hash, int(success), message, utc_now()),
    )


def mark_submitted(conn: sqlite3.Connection, candidate_id: str) -> None:
    remove_from_selection(conn, candidate_id)
    conn.execute("UPDATE candidates SET submitted_at=? WHERE id=?", (utc_now(), candidate_id))


def exclude_deleted_on_site(conn: sqlite3.Connection, candidate_id: str, reason: str) -> None:
    """种子已被站点删除：移出清单并把候选标记为排除。"""
    remove_from_selection(conn, candidate_id)
    conn.execute("UPDATE candidates SET eligibility='excluded',exclusion_reason=? WHERE id=?", (reason, candidate_id))
