"""站况 → 升级：已入馆影片里升级搜索找到的更高画质候选。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..database import connect
from ..queries import upgrades as queries
from ..responses import UpgradeFilm
from ..util import to_int

router = APIRouter()


@router.get("/api/upgrades", response_model=list[UpgradeFilm])
async def upgrades() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = queries.films_with_upgrade_candidates(conn)
    return [
        {
            "id": to_int(row["id"]),
            "rank_no": row["rank_no"],
            "title": row["tmdb_title"] or row["chinese_title"] or row["original_title"],
            "year": to_int(row["tmdb_year"] or row["year"]) or None,
            "from_tier": to_int(row["upgrade_above"]),
            "candidate_count": to_int(row["candidate_count"]),
            "selected_count": to_int(row["selected_count"] or 0),
            "best_resolution": row["best_resolution"],
            "searched_at": row["searched_at"],
        }
        for row in rows
    ]
