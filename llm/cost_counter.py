"""
成本計數器（llm/cost_counter.py）
追蹤每日/每月 LLM 呼叫次數，超過閾值自動通知 Kevin。額度數字由 config/thresholds.json 的 llm 區塊控制。
"""
import json
from datetime import date, datetime
from typing import Optional

from config import STATE_DIR, config


def _today_path():
    return STATE_DIR / f"calls_{date.today().isoformat()}.json"


def _month_path():
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

    llm_cfg = config.llm_config()
    daily_limit = llm_cfg.get("daily_call_limit", 50)
    monthly_alert = llm_cfg.get("monthly_call_alert", 1000)

    return {
        "daily_used": today["calls"],
        "daily_remaining": max(0, daily_limit - today["calls"]),
        "monthly_total": month["calls"],
        "alert": _check_alert(today["calls"], month["calls"], daily_limit, monthly_alert),
    }


def _check_alert(daily: int, monthly: int, daily_limit: int, monthly_alert: int) -> Optional[str]:
    if daily >= daily_limit:
        return "🔴 當日 LLM 額度用盡，請 Kevin 評估是否放寬"
    if monthly >= monthly_alert:
        return "🟠 當月 LLM 呼叫已破上限，建議檢視觸發嚴重度"
    if daily >= daily_limit * 0.8:
        return f"🟡 當日已用 {daily}/{daily_limit}（80%）"
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "simulate_call":
        print(json.dumps(record_call("2883", "TEST"), ensure_ascii=False, indent=2))
    else:
        print("今日:", load_today())
        print("本月:", load_month())
