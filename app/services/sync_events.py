"""统一同步的事件入口：接收 MoviePilot、Transmission、Emby 主动推送的事件。

三方各有各的格式，这里把它们整理成同一种“提示”（哪个种子、哪部影片有动静），再触发同步。
事件只是提示：影片状态仍由同步向各系统重新读取后计算，所以伪造或重复的事件改变不了任何状态。
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

from ..config import save_runtime_settings, settings
from ..database import connect
from ..logs import event_logger
from ..queries import downloads as download_queries
from ..queries import films as film_queries
from ..security import safe_error, token_matches
from ..util import looks_like_series, media_kind, to_int, utc_now
from . import sync
from .library import recheck_library_states

SOURCES = ("moviepilot", "transmission", "emby")
HASH_PATTERN = re.compile(r"[0-9a-f]{40}")
# 多个事件在这段时间内合并成一次同步（例如一部影片的添加、整理事件接连到达）。
EVENT_DEBOUNCE_SECONDS = 3.0
TOKEN_FAILURE_WINDOW_SECONDS = 60
TOKEN_FAILURE_LIMIT = 10
RECENT_LIMIT = 20

# MoviePilot 的 Webhook 插件会推送全部事件，这里只关心与下载、整理有关的。
MOVIEPILOT_KINDS = {
    "download.added": "added",
    "download.deleted": "removed",
    "transfer.complete": "organized",
    "transfer.failed": "organize_failed",
}


@dataclass
class Hint:
    source: str
    kind: str
    hash: str | None = None
    tmdb_id: int | None = None
    keys: list[str] | None = None
    media_type: str | None = None  # movie / tv；事件里的 TMDB 编号要带上类型才可靠


_failures: dict[str, list[float]] = {}
_pending: asyncio.Task[None] | None = None


def sync_token() -> str:
    """事件地址里的密钥：首次使用时生成并保存，之后保持不变。"""
    if not settings.sync_token:
        save_runtime_settings({"sync_token": secrets.token_urlsafe(24)})
    return settings.sync_token


def reset_token() -> str:
    """换一个新的事件密钥；三方里填的旧地址随即失效，需要重新填写。"""
    save_runtime_settings({"sync_token": secrets.token_urlsafe(24)})
    _failures.clear()
    return settings.sync_token


def authorized(provided: str | None, client_ip: str) -> bool:
    """校验事件地址里的密钥；同一客户端反复失败会被限速。"""
    sync_token()
    now = time.monotonic()
    failures = [stamp for stamp in _failures.get(client_ip, []) if now - stamp < TOKEN_FAILURE_WINDOW_SECONDS]
    if len(failures) >= TOKEN_FAILURE_LIMIT:
        _failures[client_ip] = failures
        return False
    if token_matches(provided, settings.sync_token):
        return True
    failures.append(now)
    _failures[client_ip] = failures
    return False


def _body(raw: bytes, query: dict[str, str]) -> dict[str, Any]:
    """请求体可能是 JSON、表单（Transmission 脚本）或为空（参数都在地址里）。"""
    data: dict[str, Any] = dict(query)
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return data
    try:
        parsed = json.loads(text)
    except ValueError:
        data.update({key: values[0] for key, values in parse_qs(text).items() if values})
        return data
    if isinstance(parsed, dict):
        data.update(parsed)
    return data


def _find(node: Any, names: tuple[str, ...], depth: int = 0) -> Any:
    """在嵌套的字典里找第一个叫这些名字、且有值的键。"""
    if depth > 6:
        return None
    if isinstance(node, dict):
        for name in names:
            if node.get(name) not in (None, "", 0):
                return node[name]
        for value in node.values():
            found = _find(value, names, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node[:20]:
            found = _find(value, names, depth + 1)
            if found is not None:
                return found
    return None


def _hash(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    return text if HASH_PATTERN.fullmatch(text) else None


def parse(source: str, raw: bytes, query: dict[str, str]) -> Hint | None:
    """把一个事件整理成提示；不认识或与本系统无关的事件返回 None。"""
    data = _body(raw, query)
    if source == "moviepilot":
        kind = MOVIEPILOT_KINDS.get(str(data.get("type") or ""))
        if not kind:
            return None
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        media = _find(payload, ("mediainfo", "media_info"))
        return Hint(
            source, kind, _hash(payload.get("download_hash") or payload.get("hash")),
            to_int(_find(media, ("tmdb_id",))) or None if isinstance(media, dict) else None, sorted(payload)[:12],
            media_kind(_find(media, ("type", "media_type"))) if isinstance(media, dict) else None,
        )
    if source == "transmission":
        kind = {"added": "added", "done": "done"}.get(str(data.get("event") or "").strip().casefold())
        return Hint(source, kind, _hash(data.get("hash"))) if kind else None
    if source == "emby":
        event = str(data.get("Event") or data.get("event") or "").strip().casefold()
        item = data.get("Item") if isinstance(data.get("Item"), dict) else {}
        if event not in {"library.new", "item.added"} or str(item.get("Type") or "Movie") != "Movie":
            return None
        providers = item.get("ProviderIds") if isinstance(item.get("ProviderIds"), dict) else {}
        tmdb = next((value for key, value in providers.items() if str(key).casefold() == "tmdb"), None)
        return Hint(source, "library_new", None, to_int(tmdb) or None)
    return None


def _record(hint: Hint) -> None:
    """事件记入数据库（保留 30 天），重启后联动页仍能看到最近收到过什么。"""
    with connect() as conn:
        conn.execute(
            "INSERT INTO sync_events(source,kind,hash,tmdb_id,fields_json,received_at) VALUES(?,?,?,?,?,?)",
            (hint.source, hint.kind, hint.hash[:8] if hint.hash else None, hint.tmdb_id,
             json.dumps(hint.keys) if hint.keys else None, utc_now()),
        )


# Emby 入库事件按 TMDB 编号合并：同一批事件攒在一起、只跑一个后台复查，查询并发由复查本身限制，
# 事件集中到达（整库刷新、批量入库）时不会叠出一堆并行的 Emby 请求。
_emby_pending: set[int] = set()
_emby_drain: asyncio.Task[None] | None = None


async def _drain_emby() -> None:
    await asyncio.sleep(EVENT_DEBOUNCE_SECONDS)
    while _emby_pending:
        tmdb_ids = sorted(_emby_pending)
        _emby_pending.clear()
        with connect() as conn:
            ids = sorted({item_id for tmdb_id in tmdb_ids for item_id in film_queries.item_ids_by_tmdb(conn, tmdb_id)})
        try:
            await recheck_library_states(ids)
        except Exception as exc:
            event_logger().warning("sync_event_failed", extra={"error": safe_error(exc)})


def _after_emby(tmdb_id: int) -> None:
    """Emby 报告某部影片入库：只向 Emby 复查片单里对应的影片（合并同批事件）。"""
    global _emby_drain
    _emby_pending.add(tmdb_id)
    if _emby_drain is None or _emby_drain.done():
        _emby_drain = asyncio.get_running_loop().create_task(_drain_emby())
        sync._background.add(_emby_drain)
        _emby_drain.add_done_callback(sync._background.discard)


async def _after_delay() -> None:
    await asyncio.sleep(EVENT_DEBOUNCE_SECONDS)
    # 事件到达时同步正好在跑，它读到的可能是事件之前的状态：等它结束后再同步一次。
    if sync.running():
        await sync.wait()
    await sync.reconcile()


def _schedule_sync() -> None:
    global _pending
    if _pending is not None and not _pending.done():
        return

    async def run() -> None:
        try:
            await _after_delay()
        except Exception as exc:
            event_logger().warning("sync_event_failed", extra={"error": safe_error(exc)})

    _pending = asyncio.get_running_loop().create_task(run())


def handle(hint: Hint) -> None:
    """记录事件并触发相应的同步（立即返回，同步在后台进行）。"""
    _record(hint)
    if hint.source == "moviepilot" and hint.hash and hint.tmdb_id and hint.kind in {"added", "organized"}:
        # MoviePilot 自己识别的影片编号最准：记到种子上，片单影片立刻对得上。
        with connect() as conn:
            download_queries.remember_torrent_media(
                conn, hint.hash, title=None, year=None, tmdb_id=hint.tmdb_id, poster_path=None, source="moviepilot",
                media_type=hint.media_type,
            )
    if hint.source == "emby" and hint.tmdb_id:
        _after_emby(hint.tmdb_id)
        return
    _schedule_sync()


def stats() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT source,COUNT(*) AS count,MAX(received_at) AS last_at FROM sync_events GROUP BY source",
        ).fetchall()
        recent = conn.execute(
            "SELECT received_at,source,kind,hash,tmdb_id,fields_json FROM sync_events ORDER BY id DESC LIMIT ?", (RECENT_LIMIT,),
        ).fetchall()
        last_kind = {
            row["source"]: row["kind"] for row in conn.execute(
                "SELECT source,kind FROM sync_events WHERE id IN (SELECT MAX(id) FROM sync_events GROUP BY source)",
            ).fetchall()
        }
    by_source = {row["source"]: row for row in rows}
    return {
        "events": {
            source: {
                "count": to_int(by_source[source]["count"]) if source in by_source else 0,
                "last_at": by_source[source]["last_at"] if source in by_source else None,
                "last_kind": last_kind.get(source),
            }
            for source in SOURCES
        },
        "recent": [
            {"at": row["received_at"], "source": row["source"], "kind": row["kind"], "hash": row["hash"],
             "tmdb_id": row["tmdb_id"], "fields": json.loads(row["fields_json"]) if row["fields_json"] else None}
            for row in recent
        ],
    }
