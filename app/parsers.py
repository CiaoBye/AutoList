"""PT 站点 HTML 表格解析器与数值工具（审计 2-9：从 clients.py 拆出）。"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any

from .config import settings
from .util import to_float, to_int


class NexusTableParser(HTMLParser):
    """Collect torrent rows while preserving outer cells across nested tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self.stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "tr":
            self.stack.append({"cells": [], "td_depth": 0, "links": [], "anchors": [], "free": False})
            return
        for row in self.stack:
            if tag.lower() == "td":
                row["td_depth"] += 1
                if row["td_depth"] == 1:
                    row["cells"].append([])
            elif tag.lower() == "a":
                link = {"href": html.unescape(attributes.get("href", "")), "title": attributes.get("title", ""), "text": []}
                row["links"].append(link)
                row["anchors"].append(link)
            elif tag.lower() == "form" and attributes.get("action"):
                # 部分站点（如站点K）的下载按钮是表单，下载地址在 action 里。
                row["links"].append({"href": html.unescape(attributes["action"]), "title": "", "text": []})
            marker = " ".join((attributes.get("class", ""), attributes.get("src", "")))
            if re.search(r"(?:^|[\s_/.-])(pro_free|free2up|freeleech|free|2up)(?:[\s_/.-]|$)", marker, re.I):
                row["free"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "tr":
            if self.stack:
                self.rows.append(self.stack.pop())
            return
        for row in self.stack:
            if tag.lower() == "td" and row["td_depth"]:
                row["td_depth"] -= 1
            elif tag.lower() == "a" and row["anchors"]:
                row["anchors"].pop()

    def handle_data(self, data: str) -> None:
        for row in self.stack:
            if row["td_depth"] and row["cells"]:
                row["cells"][-1].append(data)
            if row["anchors"]:
                row["anchors"][-1]["text"].append(data)


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
