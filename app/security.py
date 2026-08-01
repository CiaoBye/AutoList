import hmac
import re
from typing import Any


import hashlib
import hmac
import re
from typing import Any


SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|cookie|passkey|authorization|password|passwd|secret)\b[\"']?\s*[:=]\s*)([\"']?)([^\s,&}\]#\"']+|[^\"']+)(\2)"
)
BEARER_TOKEN = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
URL_USERINFO = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
SENSITIVE_HEADER_LINE = re.compile(r"(?im)^(\s*(?:Authorization|Cookie|Set-Cookie)\s*:\s*).+$")
# PT 站点下载/详情链接常用裸 key= 携带 passkey 类秘密，在 URL 上下文中统一脱敏（保留参数名）。
URL_SENSITIVE_PARAM = re.compile(
    r"(?i)(https?://[^\s<>\"']*[?&])(key|token|passkey|secret|apikey|api[_-]?key)=[^&\s<>\"']+"
)


def sanitize_sensitive_text(value: Any, limit: int = 500) -> str:
    """Return a user-safe diagnostic string without credentials or tracker secrets."""
    text = str(value or "")
    text = URL_USERINFO.sub(r"\1***:***@", text)
    text = URL_SENSITIVE_PARAM.sub(r"\1\2=***", text)
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
