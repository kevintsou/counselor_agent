"""
軍師系統 — MCP Server (mcp_server/server.py)
==============================================
獨立進程,不依賴 sentinel 的即時 tick 迴圈(維持 GIL 隔離原則)。
透過 stdio 對外提供 MCP 工具,讓 LLM client(Claude / Claude Code / 其他 MCP host)
可以查詢軍師系統的狀態、盤後指標、告警紀錄,也能直接修改 watchlist 與策略門檻。

啟動:
    python -m mcp_server.server
或註冊進 .mcp.json 讓 Claude Code 自動啟動(見專案根目錄 .mcp.json)。
"""
import logging

from mcp.server.fastmcp import FastMCP

from . import tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("counselor.mcp")

mcp = FastMCP("counselor-agent")


# ===================== 查詢 =====================
@mcp.tool()
def get_watchlist() -> list[dict]:
    """取得目前監控中的股票清單(代號/名稱/成本/持股/類股/備註)。"""
    return tools.get_watchlist()


@mcp.tool()
def get_config() -> dict:
    """取得完整系統設定(watchlist + 策略門檻 + PriceMonitor + 健康檢查 + LLM 額度)。修改設定前建議先呼叫這個。"""
    return tools.get_config()


@mcp.tool()
def get_snapshot(symbol: str) -> dict:
    """即時查詢某檔股票的最新成交快照(價/量),會短暫連線 Shioaji。"""
    return tools.get_snapshot(symbol)


@mcp.tool()
def get_indicators(symbol: str, trade_date: str = "") -> dict:
    """讀取某檔某日的盤後指標(組 A 量能/組 B 三大法人/組 C 融資券/組 D 大盤)。trade_date 留空 = 今天。"""
    return tools.get_indicators(symbol, trade_date or None)


@mcp.tool()
def get_recent_alerts(limit: int = 20) -> list[dict]:
    """取得最近的系統告警/軍師密令/價格變動通知紀錄。"""
    return tools.get_recent_alerts(limit)


@mcp.tool()
def get_sentinel_status() -> dict:
    """查詢 sentinel 主進程健康狀態:心跳時間、訂閱清單、子進程存活、broker 連線狀態。"""
    return tools.get_sentinel_status()


@mcp.tool()
def get_backtrack_report(symbol: str, trade_date: str = "") -> dict:
    """讀取某檔某日的完整盤後分析報告(Markdown 全文)。trade_date 留空 = 今天。"""
    return tools.get_backtrack_report(symbol, trade_date or None)


# ===================== 控制 =====================
@mcp.tool()
def add_watchlist_symbol(symbol: str, name: str = "", cost: float = 0, shares: int = 0,
                          sector: str = "", notes: str = "") -> dict:
    """新增一檔監控股票。sentinel 會在下一次熱重載(約 10 秒內)自動訂閱,不需重啟。"""
    return tools.add_watchlist_symbol(symbol, name, cost, shares, sector, notes)


@mcp.tool()
def remove_watchlist_symbol(symbol: str) -> dict:
    """從監控清單移除一檔股票。"""
    return tools.remove_watchlist_symbol(symbol)


@mcp.tool()
def trigger_backtrack(symbol: str = "", trade_date: str = "") -> dict:
    """手動觸發盤後分析。symbol 留空 = 對所有監控股各跑一輪(較耗時,含即時 API 呼叫)。"""
    return tools.trigger_backtrack(symbol or None, trade_date or None)


@mcp.tool()
def update_strategy_threshold(rule: str, params: dict) -> dict:
    """修改 R1/R2/R3/R4 觸發策略的門檻參數(部分更新)。

    例如 rule="R1", params={"min_qty": 60, "min_count": 6}。
    R1/R2 欄位: window_sec, min_qty, min_count
    R3 欄位: window_sec, amount_divisor, cooldown_sec
    R4 欄位: window_sec, min_qty, trigger_count
    """
    return tools.update_strategy_threshold(rule, params)


@mcp.tool()
def update_price_monitor_config(params: dict) -> dict:
    """修改 PriceMonitor 設定。欄位: interval_sec(檢查間隔秒), alert_ticks(觸發推播的檔位數)。"""
    return tools.update_price_monitor_config(params)


@mcp.tool()
def update_health_check_config(params: dict) -> dict:
    """修改健康檢查/watchdog 設定。欄位: interval_sec, no_tick_alert_sec, no_tick_resub_sec, max_reconnect_fails, session_recovery_grace_sec, alert_cooldown_sec, reconnect_ok_cooldown_sec。"""
    return tools.update_health_check_config(params)


@mcp.tool()
def update_llm_config(params: dict) -> dict:
    """修改 LLM 額度與呼叫參數。欄位: daily_call_limit, monthly_call_alert, max_tokens_realtime, max_tokens_backtrack, temperature。"""
    return tools.update_llm_config(params)


@mcp.tool()
def update_config_path(dotted_path: str, value) -> dict:
    """通用設定寫入(escape hatch),用點號路徑直接改任何 thresholds.json 欄位。例: dotted_path="signal_cooldown_sec", value=180。"""
    return tools.update_config_path(dotted_path, value)


# ===================== 原始資料 =====================
@mcp.tool()
def query_ticks(symbol: str, trade_date: str, limit: int = 200) -> list[dict]:
    """讀取某檔某日的逐筆成交原始資料(SQLite 快取,limit 避免單次回傳過大)。"""
    return tools.query_ticks(symbol, trade_date, limit)


@mcp.tool()
def query_institutional(trade_date: str) -> dict:
    """讀取某日三大法人買賣金額(全市場,非個股)。"""
    return tools.query_institutional(trade_date)


@mcp.tool()
def query_margin_short(symbol: str, trade_date: str) -> dict:
    """讀取某檔某日融資融券餘額與增減。"""
    return tools.query_margin_short(symbol, trade_date)


@mcp.tool()
def query_market_index(trade_date: str) -> dict:
    """讀取某日加權指數收盤/漲跌/成交量。"""
    return tools.query_market_index(trade_date)


def main():
    log.info("🔌 軍師 MCP server 啟動中(stdio transport)...")
    mcp.run()


if __name__ == "__main__":
    main()
