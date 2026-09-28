"""CookieCloud on-disk blob helpers."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..config import settings
from ..cookiecloud import decrypt_cookiecloud
from ..schemas import COOKIECLOUD_KEY_PATTERN
from ..security import safe_error


def cookiecloud_file(uuid_value: str) -> Path:
    if not re.fullmatch(COOKIECLOUD_KEY_PATTERN, str(uuid_value or "")):
        raise HTTPException(422, "CookieCloud 用户 KEY 格式无效")
    directory = Path(settings.data_dir) / "cookiecloud"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{uuid_value}.json"


def stored_cookiecloud_payload() -> dict[str, Any]:
    if not settings.cookiecloud_key or not settings.cookiecloud_password:
        raise HTTPException(422, "请先在设置中配置 CookieCloud 用户 KEY 与端对端密码")
    path = cookiecloud_file(settings.cookiecloud_key)
    if not path.exists():
        raise HTTPException(404, "尚未收到 Chrome CookieCloud 数据，请在扩展中执行一次同步")
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        return decrypt_cookiecloud(settings.cookiecloud_key, settings.cookiecloud_password, stored["encrypted"], stored.get("crypto_type", "legacy"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"CookieCloud 解密失败：{safe_error(exc)}") from exc


async def fetch_remote_cookiecloud(server_url: str, uuid_value: str, password: str) -> dict[str, Any]:
    """Fetch encrypted CookieCloud data from remote server (e.g. MoviePilot) and decrypt."""
    import httpx
    from ..util import safe_request

    if not uuid_value or not password:
        raise HTTPException(422, "请先在设置中配置 CookieCloud 用户 KEY 与端对端密码")
    path = cookiecloud_file(uuid_value)  # 先校验 KEY，再拼接远程路径
    clean_server = str(server_url or "").strip().rstrip("/")
    if not clean_server:
        raise HTTPException(422, "CookieCloud 服务器地址无效")

    if clean_server.endswith("/cookiecloud"):
        candidate_urls = [f"{clean_server}/get/{uuid_value}"]
    else:
        candidate_urls = [f"{clean_server}/get/{uuid_value}", f"{clean_server}/cookiecloud/get/{uuid_value}"]

    last_error: Exception | None = None
    data: dict[str, Any] | None = None

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        for target_url in candidate_urls:
            try:
                response = await safe_request(
                    client,
                    "GET",
                    target_url,
                    label="CookieCloud 服务器",
                    allow_private=True,
                )
                if response.status_code == 200:
                    resp_data = response.json()
                    if isinstance(resp_data, dict) and resp_data.get("encrypted"):
                        data = resp_data
                        break
                elif response.status_code == 404:
                    continue
                else:
                    response.raise_for_status()
            except Exception as exc:
                last_error = exc

    if not data or not isinstance(data, dict) or not data.get("encrypted"):
        reason = safe_error(last_error) if last_error else "未从服务器获取到有效 Cookie 密文，请确认服务器地址、用户 KEY 与端对端密码正确"
        raise HTTPException(502, f"拉取远程 CookieCloud 失败：{reason}")

    decrypted = decrypt_cookiecloud(uuid_value, password, data["encrypted"], data.get("crypto_type", "legacy"))

    save_blob = {"encrypted": data["encrypted"], "crypto_type": data.get("crypto_type", "legacy")}
    # 与扩展推送一致：临时文件写入后原子替换，避免并发读取到半截文件。
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(json.dumps(save_blob, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

    return decrypted
