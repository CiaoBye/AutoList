"""CookieCloud on-disk blob helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..config import settings
from ..cookiecloud import decrypt_cookiecloud
from ..security import safe_error


def cookiecloud_file(uuid_value: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,128}", uuid_value):
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

