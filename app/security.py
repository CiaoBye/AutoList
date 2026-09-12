import hashlib
import hmac
import os
import re
import time
from typing import Any
from urllib.parse import unquote_plus, urlparse


SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|cookie|passkey|authorization|password|passwd|secret)\b[\"']?\s*[:=]\s*)([\"']?)([^\s,&}\]#\"']+|[^\"']+)(\2)"
)
BEARER_TOKEN = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
URL_USERINFO = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
SENSITIVE_HEADER_LINE = re.compile(r"(?im)^(\s*(?:Authorization|Cookie|Set-Cookie)\s*:\s*).+$")
# PT 站点下载/详情链接常用裸 key= 携带 passkey 类秘密，在 URL 上下文中统一脱敏（保留参数名）。
DIAGNOSTIC_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
URL_QUERY_PARAM = re.compile(r"([?&])([^=&#?]+)=([^&#]*)")
SENSITIVE_QUERY_KEY = re.compile(
    r"(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|cookie|passkey|authorization|password|passwd|secret|key)",
    re.I,
)
MEDIA_SIGNATURE_TTL_SECONDS = 5 * 60
MEDIA_PATH = re.compile(r"^/api/(?:sites/\d+/icon|playlist-items/\d+/poster)$")


def sanitize_sensitive_text(value: Any, limit: int = 500) -> str:
    """Return a user-safe diagnostic string without credentials or tracker secrets."""
    text = str(value or "")
    text = URL_USERINFO.sub(r"\1***:***@", text)
    def redact_url(match: re.Match[str]) -> str:
        def redact_parameter(parameter: re.Match[str]) -> str:
            if SENSITIVE_QUERY_KEY.fullmatch(unquote_plus(parameter.group(2)).strip()):
                return f"{parameter.group(1)}{parameter.group(2)}=***"
            return parameter.group(0)
        return URL_QUERY_PARAM.sub(redact_parameter, match.group(0))

    text = DIAGNOSTIC_URL.sub(redact_url, text)
    text = BEARER_TOKEN.sub(r"\1***", text)
    text = SENSITIVE_HEADER_LINE.sub(r"\1***", text)

    def replace_assignment(match: re.Match[str]) -> str:
        quote = match.group(2) or ""
        return f"{match.group(1)}{quote}***{quote}"

    text = SENSITIVE_ASSIGNMENT.sub(replace_assignment, text)
    return text[:limit]


def safe_error(exc: Exception, limit: int = 500) -> str:
    return sanitize_sensitive_text(str(exc) or type(exc).__name__, limit)


def token_matches(provided: str | None, expected: str | None) -> bool:
    """Constant-time compare for optional access tokens via SHA-256 digests.
    Hashing first removes the token-length timing side channel and keeps the
    comparison constant-time regardless of input lengths.
    """
    left = str(provided or "").encode("utf-8")
    right = str(expected or "").encode("utf-8")
    if not left or not right:
        return False
    return hmac.compare_digest(hashlib.sha256(left).digest(), hashlib.sha256(right).digest())


def extract_access_token(authorization: str | None, header_token: str | None = None) -> str:
    """Accept Authorization: Bearer <token> or X-AutoList-Token."""
    if header_token and header_token.strip():
        return header_token.strip()
    auth = str(authorization or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def is_signed_media_path(path: str) -> bool:
    return bool(MEDIA_PATH.fullmatch(str(path or "")))


def _media_signature_value(path: str, expires_at: int) -> str:
    token = os.getenv("AUTOLIST_ACCESS_TOKEN", "").strip()
    message = f"{path}\n{expires_at}".encode("utf-8")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signed_media_url(path: str, now: int | None = None) -> str:
    """Create a short-lived HMAC URL for browser image requests.

    Browsers cannot attach ``X-AutoList-Token`` to a normal ``<img>`` request.
    The URL carries only an expiring signature, never the access token itself.
    The signature covers only the request path (never the query), matching the
    middleware contract that verifies against ``request.url.path``; cache
    parameters like ``tag`` stay outside the signed material.
    """
    if not os.getenv("AUTOLIST_ACCESS_TOKEN", "").strip():
        return path
    sign_path = urlparse(path).path or str(path)
    expires_at = int(now if now is not None else time.time()) + MEDIA_SIGNATURE_TTL_SECONDS
    signature = _media_signature_value(sign_path, expires_at)
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}expires={expires_at}&signature={signature}"


def media_signature_matches(
    path: str, expires: str | None, signature: str | None, now: int | None = None,
) -> bool:
    token = os.getenv("AUTOLIST_ACCESS_TOKEN", "").strip()
    if not token or not is_signed_media_path(path):
        return False
    try:
        expires_at = int(str(expires or ""))
    except (TypeError, ValueError):
        return False
    current = int(time.time() if now is None else now)
    if expires_at < current or expires_at > current + MEDIA_SIGNATURE_TTL_SECONDS + 30:
        return False
    expected = _media_signature_value(path, expires_at)
    return hmac.compare_digest(expected, str(signature or ""))
