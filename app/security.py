import re
from typing import Any


SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|cookie|passkey|authorization|password|passwd|secret)\b[\"']?\s*[:=]\s*)([\"']?)([^\s,&}\]#\"']+|[^\"']+)(\2)"
)
BEARER_TOKEN = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
URL_USERINFO = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
SENSITIVE_HEADER_LINE = re.compile(r"(?im)^(\s*(?:Authorization|Cookie|Set-Cookie)\s*:\s*).+$")


def sanitize_sensitive_text(value: Any, limit: int = 500) -> str:
    """Return a user-safe diagnostic string without credentials or tracker secrets."""
    text = str(value or "")
    text = URL_USERINFO.sub(r"\1***:***@", text)
    text = BEARER_TOKEN.sub(r"\1***", text)
    text = SENSITIVE_HEADER_LINE.sub(r"\1***", text)

    def replace_assignment(match: re.Match[str]) -> str:
        quote = match.group(2) or ""
        return f"{match.group(1)}{quote}***{quote}"

    text = SENSITIVE_ASSIGNMENT.sub(replace_assignment, text)
    return text[:limit]


def safe_error(exc: Exception, limit: int = 500) -> str:
    return sanitize_sensitive_text(str(exc) or type(exc).__name__, limit)
