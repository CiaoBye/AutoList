#!/usr/bin/env bash
# 以全新临时数据目录启动服务，供冒烟测试使用；退出时清理。
set -euo pipefail
cd "$(dirname "$0")/../.."
DATA_DIR="$(mktemp -d)"
trap 'rm -rf "$DATA_DIR"' EXIT
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY=python
export DATA_DIR
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 18599
