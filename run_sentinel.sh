#!/bin/bash
# 軍師啟動腳本 — 自動啟用 venv
cd "$(dirname "$0")"
source .venv/bin/activate
exec python sentinel.py "$@"
