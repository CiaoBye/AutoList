"""Emby library scan orchestration."""

from __future__ import annotations

import asyncio
from typing import Any

from ..clients import EmbyClient
from ..config import settings
from ..database import connect
from ..domain.titles import canonical_item_title, canonical_item_year
from ..security import safe_error
from ..util import rows_to_dicts, utc_now


async def library_details(
    emby: EmbyClient, title: str, year: int | None, tmdb_id: int | None = None, imdb_id: str | None = None,
    *, raise_errors: bool = False,
) -> tuple[str, str | None, str | None]:
    """Return ``unknown`` on lookup failure; only an explicit empty match is ``not_found``."""
    try:
        state, item = await emby.library_match(title, year, tmdb_id, imdb_id)
        if state not in {"in_library", "strm", "not_found", "unknown"}:
            return "unknown", None, None
        item_id = str(item.get("Id") or "") or None if item else None
        image_tag = str((item.get("ImageTags") or {}).get("Primary") or "") or None if item else None
        return state, item_id, image_tag
    except Exception:
        if raise_errors:
            raise
        return "unknown", None, None



async def hydrate_recent_emby_posters(items: list[dict[str, Any]]) -> None:
    """Backfill legacy Emby references for the small home-page shelf without a full rescan."""
    if not items or not settings.emby_base_url or not settings.emby_api_key:
        return
    emby = EmbyClient()
    semaphore = asyncio.Semaphore(3)

    async def hydrate(item: dict[str, Any]) -> None:
        if item.get("emby_item_id") or item.get("library_state") not in {"in_library", "strm"}:
            return
        async with semaphore:
            _, emby_item_id, image_tag = await library_details(
                emby, item.get("tmdb_title") or item.get("chinese_title") or item["original_title"],
                item.get("tmdb_year") or item.get("year"), item.get("tmdb_id"),
                item.get("tmdb_imdb_id") or item.get("imdb_id"),
            )
        if not emby_item_id:
            return
        item["emby_item_id"] = emby_item_id
        item["emby_image_tag"] = image_tag
        with connect() as conn:
            conn.execute(
                "UPDATE playlist_items SET emby_item_id=?,emby_image_tag=? WHERE id=?",
                (emby_item_id, image_tag, item["id"]),
            )

    await asyncio.gather(*(hydrate(item) for item in items))


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

        async def inspect(item: dict[str, Any]) -> tuple[int, str, str | None, str | None, str | None]:
            async with semaphore:
                try:
                    state, emby_item_id, image_tag = await library_details(
                        emby, canonical_item_title(item), canonical_item_year(item), item["tmdb_id"],
                        item["tmdb_imdb_id"] or item["imdb_id"], raise_errors=True,
                    )
                    return int(item["id"]), state, emby_item_id, image_tag, None
                except Exception as exc:
                    # 一个影片的 Emby 请求失败不应让其他影片的结果丢失；
                    # unknown 保留在条目状态，脱敏错误汇总到任务层。
                    return int(item["id"]), "unknown", None, None, safe_error(exc)

        completed = in_library = strm = 0
        errors: list[str] = []
        for future in asyncio.as_completed([inspect(item) for item in items]):
            item_id, state, emby_item_id, image_tag, error_message = await future
            completed += 1
            in_library += state == "in_library"
            strm += state == "strm"
            if error_message:
                item = next((candidate for candidate in items if int(candidate["id"]) == item_id), None)
                rank = item.get("rank_no") if item else item_id
                errors.append(f"#{rank} {error_message}")
            with connect() as conn:
                conn.execute(
                    "UPDATE playlist_items SET library_state=?,library_checked_at=?,emby_item_id=?,emby_image_tag=? WHERE id=?",
                    (state, utc_now(), emby_item_id, image_tag, item_id),
                )
            update_library_task(task_id, completed=completed, in_library=in_library, strm=strm)
        if errors:
            status = "failed" if len(errors) == len(items) else "partial"
            update_library_task(
                task_id, status=status, completed=completed, in_library=in_library, strm=strm,
                error_message="；".join(errors[:8])[:500],
            )
        else:
            update_library_task(task_id, status="completed", completed=completed, in_library=in_library, strm=strm)
    except asyncio.CancelledError as _cancel:
        update_library_task(task_id, status="cancelled")
        raise
    except Exception as exc:
        update_library_task(task_id, status="failed", error_message=safe_error(exc))
