"""候选下载上下文：加密后存数据库，服务重启不再丢失。

上下文里有带 passkey 的下载地址，因此：
- 用 AES-GCM 加密，密钥单独存在数据目录的 ``candidate-context.key``（权限 600），不进数据库，
  数据库备份（``*.bak-*``）即使外泄也拿不到下载地址；
- 不保存站点 Cookie，提交时读取站点当前的 Cookie（CookieCloud 会保持它最新）；
- 7 天后过期；提交前还会回站点确认种子仍然存在。

对外表现为一个字典（``state.raw_candidates``），寻片、加入待入馆清单与提交沿用原来的写法。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, MutableMapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from Crypto.Cipher import AES

from .config import settings

CONTEXT_TTL = timedelta(days=7)
KEY_FILE = "candidate-context.key"
# 站点 Cookie 与 UA 不落库，提交时按站点当前配置补上。
TRANSIENT_TORRENT_FIELDS = ("site_cookie",)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key() -> bytes:
    path = Path(settings.data_dir) / KEY_FILE
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        pass
    else:
        if mode != 0o600:
            # 部分 NAS 文件系统（如飞牛）创建文件时不采用 open() 给出的权限，读取时再收紧一次。
            path.chmod(0o600)
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(key)
    return key


def _key_id() -> str:
    """密钥指纹：换了密钥后，旧密钥加密的上下文一律视为不存在。"""
    return hashlib.sha256(_key()).hexdigest()[:16]


def _encrypt(value: dict[str, Any]) -> bytes:
    cipher = AES.new(_key(), AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    return cipher.nonce + tag + ciphertext


def _decrypt(payload: bytes) -> dict[str, Any] | None:
    try:
        nonce, tag, ciphertext = payload[:16], payload[16:32], payload[32:]
        cipher = AES.new(_key(), AES.MODE_GCM, nonce=nonce)
        value = json.loads(cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8"))
    except (ValueError, KeyError, json.JSONDecodeError):
        # 密钥被替换或数据损坏：当作已过期，需要重新寻片。
        return None
    return value if isinstance(value, dict) else None


def _connect() -> sqlite3.Connection:
    from .database import connect

    return connect()


class CandidateContexts(MutableMapping[str, dict[str, Any]]):
    """按候选 ID 读写加密的下载上下文；过期或无法解密的条目视为不存在。"""

    def __getitem__(self, candidate_id: str) -> dict[str, Any]:
        with _connect() as conn:
            row = conn.execute(
                "SELECT payload FROM candidate_contexts WHERE candidate_id=? AND expires_at>? AND key_id=?",
                (candidate_id, _now().isoformat(), _key_id()),
            ).fetchone()
        value = _decrypt(bytes(row["payload"])) if row else None
        if value is None:
            raise KeyError(candidate_id)
        return value

    def __setitem__(self, candidate_id: str, value: dict[str, Any]) -> None:
        stored = dict(value)
        if isinstance(stored.get("torrent"), dict):
            stored["torrent"] = {key: item for key, item in stored["torrent"].items() if key not in TRANSIENT_TORRENT_FIELDS}
        now = _now()
        with _connect() as conn:
            conn.execute(
                """INSERT INTO candidate_contexts(candidate_id,payload,key_id,created_at,expires_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(candidate_id) DO UPDATE SET payload=excluded.payload,key_id=excluded.key_id,
                   created_at=excluded.created_at,expires_at=excluded.expires_at""",
                (candidate_id, _encrypt(stored), _key_id(), now.isoformat(), (now + CONTEXT_TTL).isoformat()),
            )

    def __delitem__(self, candidate_id: str) -> None:
        with _connect() as conn:
            deleted = conn.execute("DELETE FROM candidate_contexts WHERE candidate_id=?", (candidate_id,)).rowcount
        if not deleted:
            raise KeyError(candidate_id)

    def __contains__(self, candidate_id: object) -> bool:
        if not isinstance(candidate_id, str):
            return False
        try:
            with _connect() as conn:
                return conn.execute(
                    "SELECT 1 FROM candidate_contexts WHERE candidate_id=? AND expires_at>? AND key_id=?",
                    (candidate_id, _now().isoformat(), _key_id()),
                ).fetchone() is not None
        except sqlite3.OperationalError:
            return False

    def available(self, candidate_ids: list[str]) -> set[str]:
        """一次查出哪些候选还有可用的上下文（影片状态与挑选台批量使用）。"""
        if not candidate_ids:
            return set()
        marks = ",".join("?" for _ in candidate_ids)
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT candidate_id FROM candidate_contexts WHERE expires_at>? AND key_id=? AND candidate_id IN ({marks})",  # nosec B608
                (_now().isoformat(), _key_id(), *candidate_ids),
            ).fetchall()
        return {str(row["candidate_id"]) for row in rows}

    def __iter__(self) -> Iterator[str]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT candidate_id FROM candidate_contexts WHERE expires_at>? AND key_id=?", (_now().isoformat(), _key_id()),
            ).fetchall()
        return iter([str(row["candidate_id"]) for row in rows])

    def __len__(self) -> int:
        with _connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM candidate_contexts WHERE expires_at>? AND key_id=?", (_now().isoformat(), _key_id()),
            ).fetchone()[0])

    def clear(self) -> None:
        try:
            with _connect() as conn:
                conn.execute("DELETE FROM candidate_contexts")
        except sqlite3.OperationalError:
            # 数据库尚未初始化（测试启动阶段）时没有可清理的内容。
            return

    def prune(self) -> None:
        """删除过期的、旧密钥加密的，以及候选已被清理掉的上下文。

        寻片时先写上下文、后写候选行，孤儿记录只清理一小时以前的，避免并发寻片时误删。
        """
        now = _now()
        with _connect() as conn:
            conn.execute(
                """DELETE FROM candidate_contexts WHERE expires_at<=? OR key_id<>?
                   OR (created_at<? AND candidate_id NOT IN (SELECT id FROM candidates))""",
                (now.isoformat(), _key_id(), (now - timedelta(hours=1)).isoformat()),
            )
