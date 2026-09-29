"""影片、片单与站点展示用的查询。"""

from __future__ import annotations

from typing import Any

from ..util import rows_to_dicts, to_int

FILM_ITEM_COLUMNS = """i.id,i.playlist_id,i.rank_no,i.imdb_id,i.original_title,i.year,i.chinese_title,
    i.tmdb_id,i.tmdb_title,i.tmdb_original_title,i.tmdb_year,i.tmdb_imdb_id,i.tmdb_poster_path,i.fanart_poster_url,
    i.source_tmdb_id,i.source_ref,i.fanart_backdrop_url,i.tmdb_backdrop_path,
    i.emby_item_id,i.emby_image_tag,i.library_state,i.library_checked_at"""


def playlist_summaries(conn: Any) -> list[dict[str, Any]]:
    return rows_to_dicts(conn.execute(
        """SELECT p.id,p.name,p.source_type,COUNT(i.id) AS item_count FROM playlists p
           LEFT JOIN playlist_items i ON i.playlist_id=p.id GROUP BY p.id ORDER BY p.position,p.id"""
    ).fetchall())


def playlist_items(conn: Any, playlist_id: int | None) -> list[dict[str, Any]]:
    """片单里的影片（按序号）；不指定片单时按片单顺序列出全部影片。"""
    if playlist_id is None:
        rows = conn.execute(
            f"""SELECT {FILM_ITEM_COLUMNS} FROM playlist_items i JOIN playlists p ON p.id=i.playlist_id
                ORDER BY p.position,p.id,i.rank_no"""  # nosec B608
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {FILM_ITEM_COLUMNS} FROM playlist_items i WHERE i.playlist_id=? ORDER BY i.rank_no",  # nosec B608
            (playlist_id,),
        ).fetchall()
    return rows_to_dicts(rows)


def film_item(conn: Any, item_id: int, *, with_playlist: bool = False) -> dict[str, Any] | None:
    if with_playlist:
        row = conn.execute(
            f"""SELECT {FILM_ITEM_COLUMNS}, p.name AS playlist_name FROM playlist_items i
                JOIN playlists p ON p.id=i.playlist_id WHERE i.id=?""",  # nosec B608
            (item_id,),
        ).fetchone()
    else:
        row = conn.execute(f"SELECT {FILM_ITEM_COLUMNS} FROM playlist_items i WHERE i.id=?", (item_id,)).fetchone()  # nosec B608
    return dict(row) if row else None


def film_history(conn: Any, item_id: int, limit: int = 20) -> list[dict[str, Any]]:
    return rows_to_dicts(conn.execute(
        """SELECT h.id,h.title,h.torrent_name,h.site_name,h.success,h.message,h.created_at
           FROM download_history h LEFT JOIN candidates c ON c.id=h.candidate_id
           WHERE COALESCE(h.playlist_item_id, c.playlist_item_id)=? ORDER BY h.id DESC LIMIT ?""",
        (item_id, limit),
    ).fetchall())


def search_summary(conn: Any, item_id: int) -> dict[str, Any] | None:
    """这部影片最近一次寻片的站点尝试汇总。"""
    latest = conn.execute(
        "SELECT MAX(task_id) AS task_id FROM search_attempts WHERE playlist_item_id=?", (item_id,),
    ).fetchone()
    if not latest or latest["task_id"] is None:
        return None
    summary = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                  SUM(result_count) AS results, MAX(finished_at) AS finished_at
           FROM search_attempts WHERE playlist_item_id=? AND task_id=?""",
        (item_id, latest["task_id"]),
    ).fetchone()
    return {
        "task_id": to_int(latest["task_id"]),
        "sites": to_int(summary["total"] or 0),
        "succeeded": to_int(summary["succeeded"] or 0),
        "failed": to_int(summary["failed"] or 0),
        "results": to_int(summary["results"] or 0),
        "finished_at": summary["finished_at"],
    }


# 海报与剧照查到后记在影片上：None = 还没查过，'' = 查过但没有。
ARTWORK_COLUMNS = frozenset({
    "tmdb_poster_path", "tmdb_original_language", "fanart_poster_url", "fanart_backdrop_url", "tmdb_backdrop_path",
})


def film_artwork(conn: Any, item_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT tmdb_id,emby_item_id,emby_image_tag,{','.join(sorted(ARTWORK_COLUMNS))} FROM playlist_items WHERE id=?",  # nosec B608
        (item_id,),
    ).fetchone()
    return dict(row) if row else None


def remember_artwork(conn: Any, item_id: int, tmdb_id: int, column: str, value: str) -> None:
    """记下查到的海报、剧照或原语言；影片在此期间被改识别为别的 TMDB 编号时不写入。"""
    if column not in ARTWORK_COLUMNS:
        raise ValueError(f"unknown artwork column: {column}")
    conn.execute(f"UPDATE playlist_items SET {column}=? WHERE id=? AND tmdb_id=?", (value, item_id, tmdb_id))  # nosec B608
