#!/bin/bash
# 軍師 sentinel 外部 watchdog(2026-06-04 加,2026-06-04 17:00 改 v2)
#
# 【v1 死亡迴圈 Bug 修法】原本 MAX_IDLE=120s 不分時段,結果收盤後 sentinel
# 故意 sleep 16.4 小時(設計上正確),keep_alive 把它當卡死殺掉,
# launchd 立刻重啟 → 進死循環,明早根本沒有 sentinel。
#
# 【v2 修法】加時段判斷:
#   - 開盤時段(週一到五 08:50-13:40):log 120s 沒新 → 視為卡死 → kill -KILL
#   - 收盤後(13:40-次日 08:50):不檢查(sentinel 故意 sleep,合法)
#   - 週末(週六/週日):不檢查
#
# 設計意圖:watchdog 只保護「預期活躍」時段,不打擾合法 sleep。
LOG=/Users/kjkin2006/.openclaw/workspace/projects/counselor_agent/logs/sentinel.log
LABEL=ai.openclaw.counselor-sentinel
MAX_IDLE=120
WARN_FILE=/tmp/sentinel_stuck_warned

# 取得台北時段(用 TZ 環境變數,跨平台)
WEEKDAY=$(TZ=Asia/Taipei date +%u)   # 1=週一 7=週日
HOUR=$(TZ=Asia/Taipei date +%H)
MINUTE=$(TZ=Asia/Taipei date +%M)
MIN=$((10#$HOUR * 60 + 10#$MINUTE))   # 當下分鐘數(0-1439),10# 強制十進位

# 收盤後/凌晨/週末 → 都不檢查
is_quiet_time() {
    # 週末:週六(6)、週日(7)
    if [ "$WEEKDAY" -ge 6 ]; then
        return 0
    fi
    # 收盤後 13:40(820 分)到當日 23:59(1439 分)
    if [ "$MIN" -ge 820 ]; then
        return 0
    fi
    # 隔天 00:00 到 08:50(530 分)— 凌晨也視為昨晚收盤延伸
    if [ "$MIN" -lt 530 ]; then
        return 0
    fi
    return 1
}

if is_quiet_time; then
    exit 0
fi

# ===== 開盤時段才檢查卡死 =====
PID=$(launchctl list | grep "$LABEL" | awk '{print $1}' | head -1)
if [ -z "$PID" ] || [ "$PID" = "-" ]; then
    exit 0
fi

if [ ! -f "$LOG" ]; then
    exit 0
fi

NOW=$(date +%s)
MTIME=$(stat -f %m "$LOG" 2>/dev/null || echo 0)
IDLE=$((NOW - MTIME))

if [ "$IDLE" -gt "$MAX_IDLE" ]; then
    if [ ! -f "$WARN_FILE" ]; then
        /Users/kjkin2006/.openclaw/workspace/projects/counselor_agent/.venv/bin/python -c "
import sys
sys.path.insert(0, '/Users/kjkin2006/.openclaw/workspace/projects/counselor_agent')
from herald import send_alert
send_alert('red', f'⚠️ sentinel 開盤中卡死 {IDLE}s,強制重啟 PID={PID}')
" 2>/dev/null
        touch "$WARN_FILE"
    fi
    kill -KILL $PID 2>/dev/null
    rm -f "$WARN_FILE"
fi
