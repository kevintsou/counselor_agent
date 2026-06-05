#!/bin/bash
# 軍師盤後分析啟動腳本(17:00 cron 觸發)
cd "$(dirname "$0")"
source .venv/bin/activate
exec python backtrack.py "$@" 2>&1 | tee -a logs/backtrack.log
