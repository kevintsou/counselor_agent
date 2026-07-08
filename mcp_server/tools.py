"""
軍師系統 — MCP 工具實作 (mcp_server/tools.py)
==============================================
純函式,不依賴 `mcp` 套件,方便單元測試與被其他程式呼叫。
mcp_server/server.py 負責把這裡的函式註冊成 MCP tool。

三大類:
  查詢 — watchlist / 快照 / 指標 / 告警紀錄 / sentinel 健康狀態 / 盤後報告
  控制 — 新增/移除監控股、觸發盤後分析、修改策略門檻與各項設定
  原始資料 — 逐筆 ticks / 三大法人 / 融資券 / 大盤指數
"""
import json
from datetime import date
from typing import Optional

from config import REPORTS_DIR, STATE_DIR, config


# ===================== 查詢 =====================
def get_watchlist() -> list[dict]:
    """目前監控中的股票清單。"""
    return config.stocks


def get_config() -> dict:
    """目前完整設定(watchlist + thresholds),供 LLM 修改前先讀取現況。"""
    return config.snapshot()


def get_snapshot(symbol: str) -> dict:
    """即時連線 Shioaji 抓一次快照(價格/量)。標的須在 watchlist 或為合法代號。"""
    from broker import broker
    if not broker.connect(retries=1):
        return {"error": "Shioaji 連線失敗"}
    try:
        snap = broker.get_snapshot(symbol)
        return snap or {"error": f"取不到 {symbol} 快照"}
    finally:
        broker.disconnect()


def get_indicators(symbol: str, trade_date: Optional[str] = None) -> dict:
    """讀取 SQLite 快取算出的盤後指標(組 A/B/C/D)。資料須先跑過 trigger_backtrack 或 sentinel 盤後流程。"""
    from data.indicators import calc_all
    from data.market import load_market
    from data.ticks import load_snapshot_from_db, load_ticks_from_db
    from data.twse import load_institutional, load_margin_short

    trade_date = trade_date or date.today().isoformat()
    ticks = load_ticks_from_db(symbol, trade_date)
    if not ticks:
        return {"error": f"找不到 {symbol} {trade_date} 的 ticks 快取,請先 trigger_backtrack"}
    snap = load_snapshot_from_db(symbol, trade_date)
    inst = load_institutional(trade_date)
    ms = load_margin_short(symbol, trade_date)
    market = load_market(trade_date)
    return calc_all(ticks, snap, inst, ms, market, symbol)


def get_recent_alerts(limit: int = 20) -> list[dict]:
    """最近的告警/密令/價格通知紀錄(state/alerts.jsonl)。"""
    path = STATE_DIR / "alerts.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))


def get_sentinel_status() -> dict:
    """sentinel 主進程健康狀態(心跳 / 訂閱清單 / 子進程存活 / broker 連線)。"""
    path = STATE_DIR / "sentinel_status.json"
    if not path.exists():
        return {"error": "sentinel 尚未啟動或尚未寫入狀態檔"}
    return json.loads(path.read_text(encoding="utf-8"))


def get_backtrack_report(symbol: str, trade_date: Optional[str] = None) -> dict:
    """讀取盤後分析 md 報告全文。"""
    trade_date = trade_date or date.today().isoformat()
    path = REPORTS_DIR / f"{trade_date}_{symbol}.md"
    if not path.exists():
        return {"error": f"找不到報告: {path.name}"}
    return {"path": str(path), "content": path.read_text(encoding="utf-8")}


# ===================== 控制 =====================
def add_watchlist_symbol(symbol: str, name: str = "", cost: float = 0, shares: int = 0,
                          sector: str = "", notes: str = "") -> dict:
    """新增一檔監控股(sentinel 會在下次熱重載時自動訂閱,不需重啟)。"""
    ok = config.add_stock(symbol, name, cost, shares, sector, notes)
    return {"ok": ok, "message": "已新增" if ok else f"{symbol} 已在 watchlist 中"}


def remove_watchlist_symbol(symbol: str) -> dict:
    """移除一檔監控股。"""
    ok = config.remove_stock(symbol)
    return {"ok": ok, "message": "已移除" if ok else f"{symbol} 不在 watchlist 中"}


def trigger_backtrack(symbol: Optional[str] = None, trade_date: Optional[str] = None) -> dict:
    """手動觸發盤後分析。symbol 省略 = 對 watchlist 所有標的各跑一輪(耗時較長,含 Shioaji + TWSE + LLM 呼叫)。"""
    from analysis.backtrack import run, run_all
    if symbol:
        ok = run(symbol, trade_date)
        return {symbol: ok}
    return run_all(trade_date)


def update_strategy_threshold(rule: str, params: dict) -> dict:
    """修改 R1/R2/R3/R4 其中一條規則的門檻(部分欄位更新,未提供的欄位維持原值)。

    例: update_strategy_threshold("R1", {"min_qty": 60}) 只改 min_qty,其餘沿用。
    """
    rule = rule.upper()
    if rule not in ("R1", "R2", "R3", "R4"):
        return {"ok": False, "message": "rule 必須是 R1/R2/R3/R4"}
    current = config.strategy_params(rule)
    current.update(params)
    config.update(f"strategy.{rule}", current)
    return {"ok": True, "rule": rule, "new_params": current}


def update_price_monitor_config(params: dict) -> dict:
    """修改 PriceMonitor 設定(interval_sec / alert_ticks)。"""
    current = config.price_monitor_config()
    current.update(params)
    config.update("price_monitor", current)
    return {"ok": True, "new_params": current}


def update_health_check_config(params: dict) -> dict:
    """修改健康檢查設定(interval_sec / no_tick_alert_sec / no_tick_resub_sec / max_reconnect_fails 等)。"""
    current = config.health_check_config()
    current.update(params)
    config.update("health_check", current)
    return {"ok": True, "new_params": current}


def update_llm_config(params: dict) -> dict:
    """修改 LLM 額度與參數(daily_call_limit / monthly_call_alert / max_tokens_* / temperature)。"""
    current = config.llm_config()
    current.update(params)
    config.update("llm", current)
    return {"ok": True, "new_params": current}


def update_config_path(dotted_path: str, value) -> dict:
    """通用設定寫入escape hatch,例如 dotted_path='signal_cooldown_sec', value=180。"""
    config.update(dotted_path, value)
    return {"ok": True, "path": dotted_path, "value": value}


# ===================== 原始資料 =====================
def query_ticks(symbol: str, trade_date: str, limit: int = 200) -> list[dict]:
    """讀取某檔某日的逐筆成交(SQLite 快取,須先跑過盤後抓取)。limit 避免單次回傳過大。"""
    from data.ticks import load_ticks_from_db
    rows = load_ticks_from_db(symbol, trade_date)
    return rows[:limit]


def query_institutional(trade_date: str) -> dict:
    """讀取當日三大法人買賣金額(全市場)。"""
    from data.twse import load_institutional
    return load_institutional(trade_date)


def query_margin_short(symbol: str, trade_date: str) -> dict:
    """讀取某檔當日融資融券餘額與增減。"""
    from data.twse import load_margin_short
    result = load_margin_short(symbol, trade_date)
    return result or {"error": f"找不到 {symbol} {trade_date} 融資券資料"}


def query_market_index(trade_date: str) -> dict:
    """讀取當日加權指數收盤/漲跌/成交量。"""
    from data.market import load_market
    result = load_market(trade_date)
    return result or {"error": f"找不到 {trade_date} 大盤資料"}
