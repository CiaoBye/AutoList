"""PT 站点 HTML 表格解析器与数值工具（审计 2-9：从 clients.py 拆出）。"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from .config import settings
from .util import to_float, to_int


class AccountTableParser(HTMLParser):
    """Collect simple label/value rows from tracker account pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.text: list[str] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._row is not None and self._cell is not None:
            value = re.sub(r"\s+", " ", " ".join(self._cell)).strip()
            self._row.append(value)
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)


def human_size_bytes(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return max(0, to_int(value))
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(B|Ki?B|Mi?B|Gi?B|Ti?B|Pi?B)\b", str(value or ""), re.I)
    if not match:
        return None
    units = {"b": 1, "kb": 1024, "kib": 1024, "mb": 1024**2, "mib": 1024**2,
             "gb": 1024**3, "gib": 1024**3, "tb": 1024**4, "tib": 1024**4,
             "pb": 1024**5, "pib": 1024**5}
    return to_int(to_float(match.group(1).replace(",", ".")) * units[match.group(2).lower()])


def numeric_value(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return to_float(value)
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value or "").replace(",", ""))
    return to_float(match.group(0)) if match else None


def site_proxy(site: dict[str, Any]) -> str | None:
    """Only explicitly opted-in PT sites use the configured outbound proxy."""
    return settings.outbound_proxy_url if settings.pt_proxy_enabled and site.get("proxy") and settings.outbound_proxy_url else None
