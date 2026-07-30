import base64
import hashlib
import json
from typing import Any

# PyCryptodome is maintained and required by the CookieCloud wire protocol.
from Crypto.Cipher import AES  # nosec B413
from Crypto.Util.Padding import unpad  # nosec B413


IGNORED_COOKIES = {"CookieAutoDeleteBrowsingDataCleanup", "CookieAutoDeleteCleaningDiscarded"}


def decrypt_cookiecloud(uuid: str, password: str, encrypted: str, crypto_type: str = "legacy") -> dict[str, Any]:
    # CookieCloud defines this MD5-based derivation. It is protocol compatibility, not password hashing.
    key = hashlib.md5(f"{uuid}-{password}".encode(), usedforsecurity=False).hexdigest()[:16].encode()
    raw = base64.b64decode(encrypted)
    if crypto_type == "aes-128-cbc-fixed":
        plaintext = unpad(AES.new(key, AES.MODE_CBC, b"\0" * 16).decrypt(raw), AES.block_size)
    else:
        if not raw.startswith(b"Salted__") or len(raw) < 32:
            raise ValueError("CookieCloud 密文格式无效")
        salt, ciphertext = raw[8:16], raw[16:]
        material, previous = b"", b""
        while len(material) < 48:
            previous = hashlib.md5(previous + key + salt, usedforsecurity=False).digest()
            material += previous
        plaintext = unpad(AES.new(material[:32], AES.MODE_CBC, material[32:48]).decrypt(ciphertext), AES.block_size)
    payload = json.loads(plaintext.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("CookieCloud 解密结果无效")
    return payload


def normalize_domain(value: str) -> str:
    return str(value or "").strip().lower().lstrip(".").split(":", 1)[0]


def cookie_groups(payload: dict[str, Any]) -> dict[str, str]:
    source = payload.get("cookie_data") if isinstance(payload.get("cookie_data"), dict) else payload
    grouped: dict[str, list[dict[str, Any]]] = {}
    for source_domain, cookies in source.items():
        if not isinstance(cookies, list):
            continue
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            domain = normalize_domain(cookie.get("domain") or source_domain)
            if domain:
                grouped.setdefault(domain, []).append(cookie)
    result: dict[str, str] = {}
    for domain, cookies in grouped.items():
        values = [
            f"{item['name']}={item['value']}" for item in cookies
            if item.get("name") and item.get("value") is not None and item.get("name") not in IGNORED_COOKIES
        ]
        if values and any(not value.startswith("cf_clearance=") for value in values):
            result[domain] = "; ".join(values)
    return result


def cookie_for_host(groups: dict[str, str], host: str) -> tuple[str, str] | None:
    normalized = normalize_domain(host)
    # Cookie 只可从父域应用到当前主机；子域 Cookie 不能反向写给父域。
    matches = [
        (domain, value)
        for domain, value in groups.items()
        if normalized == domain or normalized.endswith(f".{domain}")
    ]
    return max(matches, key=lambda item: len(item[0])) if matches else None
