"""
成本計數器（cost_counter.py）
追蹤每日/每月 LLM 呼叫次數，超過閾值自動通知 Kevin。
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)

DAILY_LIMIT = 50
MONTHLY_ALERT = 1000


def _today_path() -> Path:
    return STATE_DIR / f"calls_{date.today().isoformat()}.json"


def _month_path() -> Path:
    return STATE_DIR / f"calls_{date.today().strftime('%Y-%m')}.json"


def load_today() -> dict:
    p = _today_path()
    if not p.exists():
        return {"date": date.today().isoformat(), "calls": 0, "last_call": None}
    return json.loads(p.read_text())


def load_month() -> dict:
    p = _month_path()
    if not p.exists():
        return {"month": date.today().strftime("%Y-%m"), "calls": 0}
    return json.loads(p.read_text())


def record_call(symbol: str = "", trigger: str = "") -> dict:
    """記錄一次 LLM 呼叫，回傳 {daily_remaining, monthly_total, alert}"""
    today = load_today()
    month = load_month()

    today["calls"] += 1
    today["last_call"] = datetime.now().isoformat()
    today["last_symbol"] = symbol
    today["last_trigger"] = trigger
    _today_path().write_text(json.dumps(today, ensure_ascii=False, indent=2))

    month["calls"] += 1
    _month_path().write_text(json.dumps(month, ensure_ascii=False, indent=2))

    return {
        "daily_used": today["calls"],
        "daily_remaining": max(0, DAILY_LIMIT - today["calls"]),
        "monthly_total": month["calls"],
        "alert": _check_alert(today["calls"], month["calls"]),
    }


def _check_alert(daily: int, monthly: int) -> Optional[str]:
    if daily >= DAILY_LIMIT:
        return "🔴 當日 LLM 額度用盡，請 Kevin 評估是否放寬"
    if monthly >= MONTHLY_ALERT:
        return "🟠 當月 LLM 呼叫已破 1000 次，建議檢視觸發嚴重度"
    if daily >= DAILY_LIMIT * 0.8:
        return f"🟡 當日已用 {daily}/{DAILY_LIMIT}（80%）"
    return None


if __name__ == "__main__":
    # CLI 測試：python cost_counter.py [simulate_call]
    if len(sys.argv) > 1 and sys.argv[1] == "simulate_call":
        print(json.dumps(record_call("2883", "TEST"), ensure_ascii=False, indent=2))
    else:
        print("今日:", load_today())
        print("本月:", load_month())
