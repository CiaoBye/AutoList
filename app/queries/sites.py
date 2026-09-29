"""站点配置与站点搜索记录的读写。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..util import rows_to_dicts, to_int, utc_now

# 站点表单可编辑的列，按 SitePayload 的字段顺序。
SITE_FORM_COLUMNS = (
    "name", "adapter", "base_url", "api_key", "cookie", "user_agent", "priority", "timeout_seconds", "rss_url", "icon_url",
    "proxy", "render", "limit_interval", "limit_count", "enabled", "search_enabled",
)


def get_site(conn: sqlite3.Connection, site_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pt_sites WHERE id=?", (site_id,)).fetchone()


def all_sites(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows_to_dicts(conn.execute("SELECT * FROM pt_sites").fetchall())


def enabled_sites(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows_to_dicts(conn.execute("SELECT * FROM pt_sites WHERE enabled=1 ORDER BY priority,id").fetchall())


def site_display_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """候选展示用的站点名称、优先级与图标。"""
    return rows_to_dicts(conn.execute("SELECT name,priority,icon_url FROM pt_sites").fetchall())


def searchable_site_ids(conn: sqlite3.Connection) -> list[int]:
    return [
        to_int(row["id"]) for row in conn.execute(
            "SELECT id FROM pt_sites WHERE enabled=1 AND search_enabled=1 ORDER BY priority,id",
        ).fetchall()
    ]


def searchable_site_names(conn: sqlite3.Connection, last_status: str) -> list[str]:
    """参与搜索、且最近一次检测为指定状态（error / empty）的站点。"""
    return [
        str(row["name"]) for row in conn.execute(
            "SELECT name FROM pt_sites WHERE enabled=1 AND search_enabled=1 AND last_status=? ORDER BY priority,id",
            (last_status,),
        ).fetchall()
    ]


def site_count(conn: sqlite3.Connection) -> int:
    return to_int(conn.execute("SELECT COUNT(*) FROM pt_sites").fetchone()[0])


def sites_with_recent_search_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """全部站点，附最近 30 天的搜索次数、成功次数、平均耗时、结果数与最近一次搜索时间。"""
    return rows_to_dicts(conn.execute(
        """SELECT s.*,
                  COUNT(a.id) AS search_total,
                  SUM(CASE WHEN a.status='success' THEN 1 ELSE 0 END) AS search_succeeded,
                  CAST(AVG(a.duration_ms) AS INTEGER) AS search_average_ms,
                  SUM(a.result_count) AS search_result_count,
                  MAX(a.finished_at) AS search_last_attempt_at
           FROM pt_sites s
           LEFT JOIN search_attempts a ON a.site_id=s.id
             AND datetime(a.finished_at) >= datetime('now', '-30 days')
           GROUP BY s.id
           ORDER BY s.id"""
    ).fetchall())


def site_search_attempts(conn: sqlite3.Connection, site_id: int, limit: int) -> list[dict[str, Any]]:
    return rows_to_dicts(conn.execute(
        """SELECT status,result_count,duration_ms,error_code,error_message,finished_at
           FROM search_attempts WHERE site_id=? ORDER BY id DESC LIMIT ?""", (site_id, limit),
    ).fetchall())


def site_search_summary(conn: sqlite3.Connection, site_id: int) -> dict[str, Any]:
    return dict(conn.execute(
        """SELECT COUNT(*) AS total,SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                  CAST(AVG(duration_ms) AS INTEGER) AS average_ms,MAX(finished_at) AS last_attempt_at
           FROM search_attempts WHERE site_id=?""", (site_id,),
    ).fetchone())


def insert_site(conn: sqlite3.Connection, values: dict[str, Any], *, cookie_source: str | None) -> int:
    """新增站点；``cookie_source`` 非空时同时记下 Cookie 的来源与更新时间。

    站点名称重复时抛出 ``sqlite3.IntegrityError``。
    """
    columns = (*SITE_FORM_COLUMNS, "created_at", "cookie_updated_at", "cookie_source")
    now = utc_now()
    params = (*(values[column] for column in SITE_FORM_COLUMNS), now, now if cookie_source else None, cookie_source)
    return to_int(conn.execute(
        f"INSERT INTO pt_sites({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",  # nosec B608
        params,
    ).lastrowid)


def update_site(conn: sqlite3.Connection, site_id: int, values: dict[str, Any]) -> None:
    """保存站点表单并清除迁移提示；站点名称重复时抛出 ``sqlite3.IntegrityError``。"""
    assignments = ",".join(f"{column}=?" for column in SITE_FORM_COLUMNS)
    conn.execute(
        f"UPDATE pt_sites SET {assignments},migration_note=NULL WHERE id=?",  # nosec B608
        (*(values[column] for column in SITE_FORM_COLUMNS), site_id),
    )


def set_cookie_source(conn: sqlite3.Connection, site_id: int, source: str | None) -> None:
    """记下 Cookie 的来源与更新时间；Cookie 被清空时传 ``None``。"""
    conn.execute(
        "UPDATE pt_sites SET cookie_updated_at=?,cookie_source=? WHERE id=?",
        (utc_now() if source else None, source, site_id),
    )


def delete_site(conn: sqlite3.Connection, site_id: int) -> None:
    conn.execute("DELETE FROM pt_sites WHERE id=?", (site_id,))
