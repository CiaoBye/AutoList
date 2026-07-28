"""Emby library scan orchestration."""

from __future__ import annotations

import asyncio
from typing import Any

from ..clients import EmbyClient
from ..database import connect
from ..domain.titles import canonical_item_title, canonical_item_year
from ..security import safe_error
from ..state import running_library_tasks
from ..util import rows_to_dicts, utc_now


async def library_details(
    emby: EmbyClient, title: str, year: int | None, tmdb_id: int | None = None, imdb_id: str | None = None,
) -> tuple[str, str | None, str | None]:
    try:
        state, item = await emby.library_match(title, year, tmdb_id, imdb_id)
        item_id = str(item.get("Id") or "") or None if item else None
        image_tag = str((item.get("ImageTags") or {}).get("Primary") or "") or None if item else None
        return state, item_id, image_tag
    except Exception:
        return "unknown", None, None


async def library_state(
    emby: EmbyClient, title: str, year: int | None, tmdb_id: int | None = None, imdb_id: str | None = None,
) -> str:
    state, _, _ = await library_details(emby, title, year, tmdb_id, imdb_id)
    return state


def update_library_task(task_id: int, **values: Any) -> None:
    if not set(values).issubset({"status", "completed", "in_library", "strm", "error_message"}):
        raise ValueError("无效的入库任务字段")
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in values)
    with connect() as conn:
        # Dynamic column names are restricted by the allowlist above.
        conn.execute(f"UPDATE library_scan_tasks SET {assignments} WHERE id=?", (*values.values(), task_id))  # nosec B608


async def run_library_scan(task_id: int) -> None:
    try:
        with connect() as conn:
            task = conn.execute("SELECT * FROM library_scan_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return
            items = rows_to_dicts(conn.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY rank_no", (task["playlist_id"],)).fetchall())
        update_library_task(task_id, status="running")
        semaphore = asyncio.Semaphore(6)
        emby = EmbyClient()

        async def inspect(item: dict[str, Any]) -> tuple[int, str, str | None, str | None]:
            async with semaphore:
                state, emby_item_id, image_tag = await library_details(
                    emby, canonical_item_title(item), canonical_item_year(item), item["tmdb_id"],
                    item["tmdb_imdb_id"] or item["imdb_id"],
                )
                return int(item["id"]), state, emby_item_id, image_tag

        completed = in_library = strm = 0
        for future in asyncio.as_completed([inspect(item) for item in items]):
            item_id, state, emby_item_id, image_tag = await future
            completed += 1
            in_library += state == "in_library"
            strm += state == "strm"
            with connect() as conn:
                conn.execute(
                    "UPDATE playlist_items SET library_state=?,library_checked_at=?,emby_item_id=?,emby_image_tag=? WHERE id=?",
                    (state, utc_now(), emby_item_id, image_tag, item_id),
                )
            update_library_task(task_id, completed=completed, in_library=in_library, strm=strm)
        update_library_task(task_id, status="completed", completed=completed, in_library=in_library, strm=strm)
    except asyncio.CancelledError:
        update_library_task(task_id, status="cancelled")
        raise
    except Exception as exc:
        update_library_task(task_id, status="failed", error_message=safe_error(exc))
    finally:
        running_library_tasks.pop(task_id, None)

