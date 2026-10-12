"""站况页用的查询：站点搜索失败记录、每日搜索次数与近 30 天的提交结果。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..util import rows_to_dicts


def recent_attempts(conn: sqlite3.Connection, site_id: int, limit: int) -> list[dict[str, Any]]:
    """某个站点最近的搜索记录（新的在前），用来数连续失败与找最近一次成功。"""
    return rows_to_dicts(conn.execute(
        """SELECT status,error_code,error_message,finished_at FROM search_attempts
           WHERE site_id=? ORDER BY id DESC LIMIT ?""", (site_id, limit),
    ).fetchall())


def attempt_days(conn: sqlite3.Connection, days: int) -> dict[int, dict[str, dict[str, int]]]:
    """站点 → 日期 → {total, failed}：最近 ``days`` 天每天的搜索次数与失败次数。"""
    result: dict[int, dict[str, dict[str, int]]] = {}
    for row in conn.execute(
        """SELECT site_id,date(finished_at) AS day,COUNT(*) AS total,
                  SUM(CASE WHEN status!='success' THEN 1 ELSE 0 END) AS failed
           FROM search_attempts
           WHERE site_id IS NOT NULL AND datetime(finished_at) >= datetime('now', ?)
           GROUP BY site_id,date(finished_at)""", (f"-{days} days",),
    ).fetchall():
        result.setdefault(int(row["site_id"]), {})[str(row["day"])] = {"total": int(row["total"]), "failed": int(row["failed"] or 0)}
    return result


def submission_counts(conn: sqlite3.Connection, days: int) -> dict[str, dict[str, int]]:
    """站点名 → 最近 ``days`` 天经 MoviePilot 提交的次数与成功次数。"""
    return {
        str(row["site_name"]): {"total": int(row["total"]), "succeeded": int(row["succeeded"] or 0)}
        for row in conn.execute(
            """SELECT site_name,COUNT(*) AS total,SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS succeeded
               FROM download_history
               WHERE site_name IS NOT NULL AND site_name!='' AND datetime(created_at) >= datetime('now', ?)
               GROUP BY site_name""", (f"-{days} days",),
        ).fetchall()
    }


def submitted_sites_by_hash(conn: sqlite3.Connection) -> dict[str, str]:
    """成功提交过的种子 hash → 站点名（用来把 Transmission 里的种子归到站点）。"""
    return {
        str(row["submission_hash"]).lower(): str(row["site_name"])
        for row in conn.execute(
            """SELECT submission_hash,site_name FROM download_history
               WHERE success=1 AND submission_hash IS NOT NULL AND submission_hash!='' AND site_name IS NOT NULL AND site_name!=''
               ORDER BY id"""
        ).fetchall()
    }
