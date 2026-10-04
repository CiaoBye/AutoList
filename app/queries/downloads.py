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
    poster_path: str | None, source: str, media_type: str | None = None,
) -> None:
    """记下一个种子识别出的影片。已有记录时，新来源没给的字段（如事件只带编号）保留原值；换了 TMDB 编号则整条替换。"""
    old = conn.execute("SELECT * FROM torrent_media WHERE hash=?", (torrent_hash,)).fetchone()
    if old and (tmdb_id is None or old["tmdb_id"] in (None, tmdb_id)) and (old["media_type"] in (None, media_type) or media_type is None):
        title = title or old["title"]
        year = year or old["year"]
        poster_path = poster_path or old["poster_path"]
        tmdb_id = tmdb_id or old["tmdb_id"]
        media_type = media_type or old["media_type"]
    conn.execute(
        """INSERT INTO torrent_media(hash,title,year,tmdb_id,poster_path,source,checked_at,media_type) VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(hash) DO UPDATE SET title=excluded.title,year=excluded.year,tmdb_id=excluded.tmdb_id,
             poster_path=excluded.poster_path,source=excluded.source,checked_at=excluded.checked_at,media_type=excluded.media_type""",
        (torrent_hash, title, year, tmdb_id, poster_path, source, utc_now(), media_type),
    )


def torrent_media_gaps(conn: sqlite3.Connection, hashes: list[str]) -> dict[tuple[str, int], list[str]]:
    """已识别出类型与 TMDB 编号、却缺片名或海报的种子，按（类型，TMDB 编号）归组。"""
    if not hashes:
        return {}
    marks = ",".join("?" for _ in hashes)
    rows = conn.execute(
        f"""SELECT hash,tmdb_id,media_type FROM torrent_media
            WHERE hash IN ({marks}) AND tmdb_id IS NOT NULL AND media_type IN ('movie','tv') AND (title IS NULL OR title='' OR poster_path IS NULL OR poster_path='')""",  # nosec B608
        hashes,
    ).fetchall()
    gaps: dict[tuple[str, int], list[str]] = {}
    for row in rows:
        gaps.setdefault((str(row["media_type"]), int(row["tmdb_id"])), []).append(str(row["hash"]))
    return gaps


def fill_torrent_media(conn: sqlite3.Connection, hashes: list[str], *, title: str | None, year: int | None, poster_path: str | None) -> None:
    """用 TMDB 的详情补齐这些种子缺的片名、年份与海报（已有的值保留）。"""
    for torrent_hash in hashes:
        conn.execute(
            """UPDATE torrent_media SET title=COALESCE(NULLIF(title,''),?), year=COALESCE(year,?),
                 poster_path=COALESCE(NULLIF(poster_path,''),?) WHERE hash=?""",
            (title, year, poster_path, torrent_hash),
        )


def torrent_poster_path(conn: sqlite3.Connection, torrent_hash: str) -> str | None:
    row = conn.execute("SELECT poster_path FROM torrent_media WHERE hash=?", (torrent_hash,)).fetchone()
    return row["poster_path"] if row else None
