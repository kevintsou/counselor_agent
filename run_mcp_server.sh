#!/bin/bash
# 軍師 MCP server 啟動腳本 — 自動啟用 venv
cd "$(dirname "$0")"
source .venv/bin/activate
exec python -m mcp_server.server "$@"
