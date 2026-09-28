"""Candidate presentation for the film detail and pick views.

候选按“同一影片 + 同一资源”折叠为一行，跨站点的同种发布保留在 ``site_options``。
下载上下文（``raw_candidates``）只在进程内保存，``context_available`` 告诉界面
该候选能否直接加入待入馆清单。
"""

from __future__ import annotations

import json
from typing import Any

from ..state import raw_candidates
from ..util import resource_fingerprint, safe_detail_url, to_int, volume_factor_value


def present_candidates(rows: list[dict[str, Any]], site_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decode stored candidate rows and fold identical releases across sites."""
    site_profiles = {str(row["name"]).lower(): dict(row) for row in site_rows}
    result = [dict(row) for row in rows]
    for item in result:
        # Existing databases may contain pre-hardening raw URLs; sanitize on
        # read as well as at insert time so old rows cannot bypass the boundary.
        item["detail_url"] = safe_detail_url(item.get("detail_url"))
        item["context_available"] = item["id"] in raw_candidates
        try:
            item["metadata"] = json.loads(item.pop("metadata_json"))
        except (TypeError, json.JSONDecodeError):
            item["metadata"] = {}
        try:
            item["score_breakdown"] = json.loads(item.get("score_breakdown") or "[]")
        except json.JSONDecodeError as _decode:
            item["score_breakdown"] = []
        item["resource_key"] = item.get("resource_key") or resource_fingerprint(item["title"], item.get("size"))
        profile = site_profiles.get(str(item.get("site_name") or "").lower(), {})
        item["site_priority"] = to_int(profile.get("priority") or 100)
        item["site_icon"] = profile.get("icon_url") or ""
        factor = volume_factor_value(item["metadata"].get("volume_factor"))
        labels = [str(label).lower() for label in item["metadata"].get("labels", [])]
        item["volume_factor"] = factor
        item["is_free"] = factor == 0 or any(label in ("free", "免费", "freeleech") for label in labels)
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in result:
        groups.setdefault((to_int(item["playlist_item_id"]), item["resource_key"]), []).append(item)
    grouped: list[dict[str, Any]] = []
    for options in groups.values():
        # 同资源跨站点折叠：优先展示做种人数最多的发布（用户可实际下载），
        # 再做种相同或缺失时按站点优先级/免费/优惠排序作为次级规则。
        options.sort(key=lambda item: (
            -to_int(item.get("seeders") or 0), item["site_priority"], 0 if item["is_free"] else 1,
            item["volume_factor"], to_int(item.get("ranking") or 0),
        ))
        primary = dict(options[0])
        primary["site_count"] = len(options)
        primary["site_options"] = [{
            "id": option["id"], "site_name": option.get("site_name"), "seeders": option.get("seeders"),
            "size": option.get("size"), "is_free": option["is_free"], "site_priority": option["site_priority"],
            "volume_factor": option["volume_factor"], "labels": option["metadata"].get("labels", []),
            "in_cart": option.get("in_cart", 0), "context_available": option["context_available"],
            "detail_url": safe_detail_url(option.get("detail_url")), "publish_time": option["metadata"].get("publish_time"),
        } for option in options]
        factor_label = "免费" if primary["volume_factor"] == 0 else (f"下载 {to_int(primary['volume_factor'] * 100)}%" if primary["volume_factor"] < 1 else "普通")
        primary["site_selection_reason"] = (
            f"站点优先级 {primary['site_priority']} · {factor_label} · {to_int(primary.get('seeders') or 0)} 做种"
        )
        primary["in_cart"] = to_int(any(option.get("in_cart") for option in options))
        grouped.append(primary)
    grouped.sort(key=lambda item: (to_int(item["rank_no"] or 0), to_int(item.get("ranking") or 0)))
    return grouped
