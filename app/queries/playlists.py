"""片单与片单条目的读写。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..util import rows_to_dicts, to_int, utc_now

# 识别结果与入馆状态：来源编号与已识别结果冲突时整组清空，交给识别任务重做。
_RESET_IDENTITY = """tmdb_id=NULL,tmdb_title=NULL,tmdb_original_title=NULL,tmdb_year=NULL,tmdb_imdb_id=NULL,tmdb_checked_at=NULL,
    tmdb_poster_path=NULL,tmdb_original_language=NULL,fanart_poster_url=NULL,fanart_backdrop_url=NULL,tmdb_backdrop_path=NULL,
    library_state='unknown',library_checked_at=NULL,emby_item_id=NULL,emby_image_tag=NULL"""


def playlist_exists(conn: sqlite3.Connection, playlist_id: int) -> bool:
    return conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone() is not None


def get_playlist(conn: sqlite3.Connection, playlist_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()


def playlists_with_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows_to_dicts(conn.execute(
        """SELECT p.*, COUNT(i.id) AS item_count, SUM(CASE WHEN i.tmdb_id IS NOT NULL THEN 1 ELSE 0 END) AS recognized_count
           FROM playlists p LEFT JOIN playlist_items i ON i.playlist_id=p.id GROUP BY p.id ORDER BY p.position,p.id"""
    ).fetchall())


def insert_playlist(conn: sqlite3.Connection, name: str, source: dict[str, Any]) -> int:
    """新片单排在最后。"""
    position = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM playlists").fetchone()[0]
    now = utc_now()
    return to_int(conn.execute(
        "INSERT INTO playlists(name,position,source_type,source_url,source_name,last_synced_at,created_at) VALUES(?,?,?,?,?,?,?)",
        (name, position, source.get("source_type"), source.get("source_url"), source.get("source_name"), now, now),
    ).lastrowid)


def rename_playlist(conn: sqlite3.Connection, playlist_id: int, name: str) -> None:
    conn.execute("UPDATE playlists SET name=? WHERE id=?", (name, playlist_id))


def playlist_ids(conn: sqlite3.Connection) -> set[int]:
    return {to_int(row[0]) for row in conn.execute("SELECT id FROM playlists")}


def set_playlist_positions(conn: sqlite3.Connection, ordered_ids: list[int]) -> None:
    conn.executemany("UPDATE playlists SET position=? WHERE id=?", [(index, playlist_id) for index, playlist_id in enumerate(ordered_ids, start=1)])


def set_playlist_automation(conn: sqlite3.Connection, playlist_id: int, *, enabled: bool, batch_size: int) -> None:
    conn.execute(
        "UPDATE playlists SET automation_enabled=?,automation_auto_select=0,automation_batch_size=? WHERE id=?",
        (to_int(enabled), batch_size, playlist_id),
    )


def set_playlist_sync(conn: sqlite3.Connection, playlist_id: int, *, enabled: bool, interval_hours: int, next_sync_at: str | None) -> None:
    conn.execute(
        "UPDATE playlists SET sync_enabled=?,sync_interval_hours=?,next_sync_at=? WHERE id=?",
        (to_int(enabled), interval_hours, next_sync_at, playlist_id),
    )


def mark_playlist_synced(conn: sqlite3.Connection, playlist_id: int, source_name: str | None) -> None:
    conn.execute("UPDATE playlists SET source_name=?,last_synced_at=? WHERE id=?", (source_name, utc_now(), playlist_id))


def delete_playlist(conn: sqlite3.Connection, playlist_id: int) -> None:
    conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))


def playlist_items_by_rank(conn: sqlite3.Connection, playlist_id: int) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY rank_no", (playlist_id,)).fetchall())


def count_playlist_items(conn: sqlite3.Connection, playlist_id: int, *, unrecognized_only: bool = False) -> int:
    """片单影片数；``unrecognized_only`` 只数识别结果不完整的影片。"""
    condition = " AND (tmdb_id IS NULL OR tmdb_title IS NULL OR tmdb_original_title IS NULL)" if unrecognized_only else ""
    return to_int(conn.execute(
        f"SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?{condition}", (playlist_id,),  # nosec B608
    ).fetchone()[0])


def emby_poster_ref(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT emby_item_id,emby_image_tag FROM playlist_items WHERE id=?", (item_id,)).fetchone()


def insert_imported_items(conn: sqlite3.Connection, playlist_id: int, items: list[dict[str, Any]]) -> None:
    # 来源自带的 TMDB 编号只记为 source_tmdb_id，识别结果 tmdb_id 由识别任务写入。
    conn.executemany(
        """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,source_tmdb_id,source_ref)
           VALUES(:playlist_id,:rank_no,:imdb_id,:original_title,:year,:chinese_title,:tmdb_id,:source_ref)""",
        [{"playlist_id": playlist_id, "tmdb_id": None, "source_ref": None, **item} for item in items],
    )


def insert_source_item(conn: sqlite3.Connection, playlist_id: int, item: dict[str, Any]) -> int:
    return to_int(conn.execute(
        """INSERT INTO playlist_items(playlist_id,rank_no,imdb_id,original_title,year,chinese_title,source_tmdb_id,source_ref)
           VALUES(?,?,?,?,?,?,?,?)""",
        (playlist_id, item["rank_no"], item.get("imdb_id"), item["original_title"], item.get("year"),
         item.get("chinese_title"), item.get("tmdb_id"), item.get("source_ref")),
    ).lastrowid)


def update_item_from_source(
    conn: sqlite3.Connection, item_id: int, item: dict[str, Any], *,
    imdb_id: str | None, source_tmdb_id: int | None, source_ref: str | None, reset_identity: bool,
) -> None:
    reset = f",{_RESET_IDENTITY}" if reset_identity else ""
    conn.execute(
        f"""UPDATE playlist_items SET rank_no=?,imdb_id=?,original_title=?,year=?,chinese_title=?,
                  source_tmdb_id=?,source_ref=?{reset} WHERE id=?""",  # nosec B608
        (item["rank_no"], imdb_id, item["original_title"], item.get("year"), item.get("chinese_title"),
         source_tmdb_id, source_ref, item_id),
    )


def shift_ranks(conn: sqlite3.Connection, playlist_id: int, offset: int) -> None:
    """把现有序号整体后移，给新序号腾出位置（序号在片单内唯一）。"""
    conn.execute("UPDATE playlist_items SET rank_no=rank_no+? WHERE playlist_id=?", (offset, playlist_id))


def delete_items(conn: sqlite3.Connection, item_ids: list[int]) -> None:
    if item_ids:
        placeholders = ",".join("?" for _ in item_ids)
        conn.execute(f"DELETE FROM playlist_items WHERE id IN ({placeholders})", item_ids)  # nosec B608


def keep_history_snapshot(conn: sqlite3.Connection, item_id: int, snapshot_json: str) -> None:
    """影片被移出片单前，把它的快照留给相关提交记录（已有快照的保留原样）。"""
    conn.execute(
        """UPDATE download_history SET playlist_item_snapshot_json=COALESCE(playlist_item_snapshot_json,?)
           WHERE playlist_item_id=? OR candidate_id IN (SELECT id FROM candidates WHERE playlist_item_id=?)""",
        (snapshot_json, item_id, item_id),
    )


def insert_recognition_task(conn: sqlite3.Connection, playlist_id: int, total: int, mode: str = "missing") -> int:
    now = utc_now()
    return to_int(conn.execute(
        "INSERT INTO recognition_tasks(playlist_id,status,total,created_at,updated_at,mode) VALUES(?,?,?,?,?,?)",
        (playlist_id, "queued", total, now, now, mode),
    ).lastrowid)


def active_recognition_task(conn: sqlite3.Connection, playlist_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id,status,total FROM recognition_tasks WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
        (playlist_id,),
    ).fetchone()


def insert_library_scan_task(conn: sqlite3.Connection, playlist_id: int, total: int) -> int:
    now = utc_now()
    return to_int(conn.execute(
        "INSERT INTO library_scan_tasks(playlist_id,status,total,created_at,updated_at) VALUES(?,?,?,?,?)",
        (playlist_id, "queued", total, now, now),
    ).lastrowid)


def active_library_scan(conn: sqlite3.Connection, playlist_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id,status FROM library_scan_tasks WHERE playlist_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
        (playlist_id,),
    ).fetchone()
