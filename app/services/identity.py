"""等待资源的影片对 TMDB 识别做二次核实：只打标记，不自动改。

等待资源常因为片单里的影片被识别成了另一部（“搜到的都不是这部电影”）。这里把当前识别与片单来源给出的
信息再对一遍：来源的 TMDB 编号、IMDb 编号（向 TMDB 反查）、片名与年份。对不上就在影片上标“识别可能有误”，
由人决定是否改。
"""

from __future__ import annotations

import json
from typing import Any

from ..clients import TMDBClient
from ..config import settings
from ..database import connect
from ..util import to_int, utc_now
from .recognition import _title_overlaps, is_year_close

# 核实结果沿用多久：识别不变时不必反复向 TMDB 查询。
IDENTITY_RECHECK_DAYS = 7
IDENTITY_CHECKS_PER_RUN = 5


def _brief(media: dict[str, Any]) -> dict[str, Any]:
    release = str(media.get("release_date") or "")[:4]
    return {
        "tmdb_id": to_int(media.get("id")),
        "title": str(media.get("title") or media.get("original_title") or "").strip(),
        "year": to_int(release) if release.isdigit() else None,
    }


async def check_identity(item: dict[str, Any]) -> dict[str, Any]:
    """核实一部影片的 TMDB 识别；返回 {"ok": bool, "reason", "suggested"}。查询失败时抛出，调用方下次再试。"""
    tmdb_id = to_int(item.get("tmdb_id"))
    source_tmdb = to_int(item.get("source_tmdb_id"))
    imdb = str(item.get("imdb_id") or "").strip()
    tmdb_imdb = str(item.get("tmdb_imdb_id") or "").strip()
    current = f"《{item.get('tmdb_title') or item.get('tmdb_original_title') or tmdb_id}》"
    client = TMDBClient()

    # 1) 片单来源直接给出的 TMDB 编号最可信。
    if source_tmdb and source_tmdb != tmdb_id:
        found = _brief(await client.movie_details(source_tmdb))
        return {"ok": False, "reason": f"片单来源给出的 TMDB 编号是 {source_tmdb}，当前识别为 {tmdb_id}（{current}）", "suggested": found}
    # 2) IMDb 编号一致：身份确认。
    if imdb and tmdb_imdb and imdb == tmdb_imdb:
        return {"ok": True}
    # 3) IMDb 编号与识别结果不一致：向 TMDB 反查这个 IMDb 编号对应哪部。
    if imdb:
        results = await client.find_by_imdb(imdb)
        if results and not any(to_int(entry.get("id")) == tmdb_id for entry in results):
            found = _brief(results[0])
            return {"ok": False, "reason": f"片单的 IMDb 编号 {imdb} 在 TMDB 上对应另一部影片，当前识别为 {current}", "suggested": found}
        if results:
            return {"ok": True}
    # 4) 没有可比的编号：比对片名与年份。
    names = [str(value) for value in (item.get("original_title"), item.get("chinese_title")) if value]
    media = {"title": item.get("tmdb_title"), "original_title": item.get("tmdb_original_title")}
    if not is_year_close(str(item.get("year") or "") or None, str(item.get("tmdb_year") or "") or None):
        return {"ok": False, "reason": f"片单年份 {item.get('year')} 与当前识别 {current} 的年份 {item.get('tmdb_year')} 不符", "suggested": None}
    if names and not any(_title_overlaps(name, media) for name in names):
        return {"ok": False, "reason": f"片单片名《{names[0]}》与当前识别 {current} 对不上", "suggested": None}
    return {"ok": True}


def identity_flag(item: dict[str, Any]) -> dict[str, Any] | None:
    """影片上要显示的“识别可能有误”标记；没核实过、核实通过或识别已换过时为 None。"""
    try:
        data = json.loads(item.get("identity_check") or "null")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("ok") is not False or to_int(data.get("tmdb_id")) != to_int(item.get("tmdb_id")):
        return None
    return {"reason": str(data.get("reason") or ""), "suggested": data.get("suggested") or None}


def _needs_check(item: dict[str, Any], now: str) -> bool:
    from datetime import datetime, timedelta

    try:
        data = json.loads(item.get("identity_check") or "null")
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict) or to_int(data.get("tmdb_id")) != to_int(item.get("tmdb_id")):
        return True
    try:
        checked = datetime.fromisoformat(str(data.get("checked_at")))
    except ValueError:
        return True
    return checked < datetime.fromisoformat(now) - timedelta(days=IDENTITY_RECHECK_DAYS)


async def verify_waiting_identities(waiting: list[dict[str, Any]]) -> int:
    """为等待资源的影片核实识别（每次同步最多几部）；返回本次新增标记的数量。"""
    if not settings.tmdb_api_key:
        return 0
    now = utc_now()
    flagged = 0
    done = 0
    for item in waiting:
        if done >= IDENTITY_CHECKS_PER_RUN:
            break
        if not to_int(item.get("tmdb_id")) or not _needs_check(item, now):
            continue
        try:
            result = await check_identity(item)
        except Exception:
            continue  # TMDB 暂时不可用：下次同步再核实
        done += 1
        record = {**result, "tmdb_id": to_int(item.get("tmdb_id")), "checked_at": now}
        with connect() as conn:
            conn.execute("UPDATE playlist_items SET identity_check=? WHERE id=? AND tmdb_id IS ?", (json.dumps(record, ensure_ascii=False), item["id"], item.get("tmdb_id")))
        if not result["ok"]:
            flagged += 1
    return flagged
