"""同一发布的判断：跨站转载的同一种子，标题写法常不同，但制作组、分辨率与体积一致。"""

from __future__ import annotations

from typing import Any

from ..util import resource_fingerprint, to_int

# 站点页面显示的体积多保留两位小数（如 13.22 GB），各站换算后会有少量出入。
SIZE_TOLERANCE = 0.01


class ReleaseClusters:
    """逐条归类资源，返回所属发布的键；同一部影片用一个实例。

    制作组与分辨率都识别到、且体积相差不超过 1% 时视为同一发布（如站点A的“…mUHD-FRDS 【卡萨布兰卡…】”
    与站点I的“Casablanca 1942 UHD BluRay 2160p … mUHD-FRDS”）；缺少任何一项时按标题与体积判断。"""

    def __init__(self) -> None:
        self._known: list[tuple[str, str, int, str]] = []

    def key(self, title: str, size: Any, group: str | None, resolution: str | None) -> str:
        size_bytes = to_int(size)
        group_key = str(group or "").strip().casefold()
        resolution_key = str(resolution or "").strip().casefold()
        if not group_key or not resolution_key or size_bytes <= 0:
            return resource_fingerprint(title, size_bytes or None)
        for known_group, known_resolution, known_size, key in self._known:
            if known_group == group_key and known_resolution == resolution_key \
                    and abs(known_size - size_bytes) <= max(known_size, size_bytes) * SIZE_TOLERANCE:
                return key
        key = f"release:{group_key}:{resolution_key}:{size_bytes}"
        self._known.append((group_key, resolution_key, size_bytes, key))
        return key
