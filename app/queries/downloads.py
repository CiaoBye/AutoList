"""下载页：提交记录与影片的对应、种子识别结果的缓存。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..util import rows_to_dicts, utc_now


def submitted_films(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """成功提交过的记录及其片单影片（新的在前）。"""
    return rows_to_dicts(conn.execute(
        """SELECT h.submission_hash,h.torrent_name,h.site_name AS submitted_site,p.*
           FROM download_history h
           LEFT JOIN candidates c ON c.id=h.candidate_id
           JOIN playlist_items p ON p.id=COALESCE(h.playlist_item_id,c.playlist_item_id)
           WHERE h.success=1 ORDER BY h.id DESC""",
    ).fetchall())


def films_by_tmdb(conn: sqlite3.Connection, tmdb_ids: list[int]) -> dict[int, dict[str, Any]]:
    """片单里这些 TMDB 编号对应的影片；同一部在多份片单里时取最早加入的。"""
    if not tmdb_ids:
        return {}
    placeholders = ",".join("?" for _ in tmdb_ids)
    rows = conn.execute(
        f"SELECT * FROM playlist_items WHERE tmdb_id IN ({placeholders}) ORDER BY id DESC", tmdb_ids,  # nosec B608
    ).fetchall()
    return {row["tmdb_id"]: dict(row) for row in rows}


def torrent_media(conn: sqlite3.Connection, hashes: list[str]) -> dict[str, dict[str, Any]]:
    if not hashes:
        return {}
    placeholders = ",".join("?" for _ in hashes)
    rows = conn.execute(f"SELECT * FROM torrent_media WHERE hash IN ({placeholders})", hashes).fetchall()  # nosec B608
    return {row["hash"]: dict(row) for row in rows}


def remember_torrent_media(
    conn: sqlite3.Connection, torrent_hash: str, *, title: str | None, year: int | None, tmdb_id: int | None,
    poster_path: str | None, source: str,
) -> None:
    conn.execute(
        """INSERT INTO torrent_media(hash,title,year,tmdb_id,poster_path,source,checked_at) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(hash) DO UPDATE SET title=excluded.title,year=excluded.year,tmdb_id=excluded.tmdb_id,
             poster_path=excluded.poster_path,source=excluded.source,checked_at=excluded.checked_at""",
        (torrent_hash, title, year, tmdb_id, poster_path, source, utc_now()),
    )


def torrent_poster_path(conn: sqlite3.Connection, torrent_hash: str) -> str | None:
    row = conn.execute("SELECT poster_path FROM torrent_media WHERE hash=?", (torrent_hash,)).fetchone()
    return row["poster_path"] if row else None
