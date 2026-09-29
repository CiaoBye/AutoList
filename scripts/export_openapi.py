"""把接口声明导出到 frontend/openapi.json（前端类型由它生成）。

用法：.venv/bin/python scripts/export_openapi.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.openapi import SNAPSHOT_PATH, openapi_document  # noqa: E402

SNAPSHOT_PATH.write_text(openapi_document(), encoding="utf-8")
print(f"已写入 {SNAPSHOT_PATH.relative_to(Path.cwd()) if SNAPSHOT_PATH.is_relative_to(Path.cwd()) else SNAPSHOT_PATH}")
