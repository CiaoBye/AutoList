"""接口声明文档：前端类型由它生成。

``frontend/openapi.json`` 是这份文档的快照（去掉随版本变化的 ``info.version``），前端构建时用
``openapi-typescript`` 把它转成 TypeScript 类型。改动接口返回格式后运行
``.venv/bin/python scripts/export_openapi.py`` 更新快照。
"""

from __future__ import annotations

import json
from pathlib import Path

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "frontend" / "openapi.json"


def openapi_document() -> str:
    from .main import app

    document = json.loads(json.dumps(app.openapi()))
    document.get("info", {}).pop("version", None)
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
