"""
軍師系統 — 統一設定層 (config.py)
==================================
單一事實來源:
  - .env                      → 密鑰(Shioaji / Telegram / CCR / RAG 路徑)
  - config/watchlist.json     → 監控股票清單 + 盤中時段
  - config/thresholds.json    → R1-R4 策略門檻 / PriceMonitor / 健康檢查 / LLM 額度

不寫死任何股票代號或門檻數字在程式碼裡;全部從這裡讀。
支援熱重載(mtime 偵測),MCP 工具與人工編輯 JSON 都能即時生效,不需重啟 sentinel。
"""
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

log = logging.getLogger("counselor.config")

CONFIG_DIR = ROOT / "config"
WATCHLIST_PATH = CONFIG_DIR / "watchlist.json"
THRESHOLDS_PATH = CONFIG_DIR / "thresholds.json"

LOGS_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
REPORTS_DIR = ROOT / "reports"
for d in (LOGS_DIR, STATE_DIR, REPORTS_DIR, CONFIG_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _get(d: dict, dotted: str, default=None):
    node = d
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _set(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    node = d
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


class ConfigStore:
    """JSON 設定讀寫 + 熱重載。執行緒安全(RLock),供 sentinel 主迴圈與 MCP server 並用。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._watchlist: dict = {}
        self._thresholds: dict = {}
        self._watchlist_mtime: float = 0.0
        self._thresholds_mtime: float = 0.0
        self._load_watchlist()
        self._load_thresholds()

    # ── 檔案 I/O ──────────────────────────────────────────────
    def _load_watchlist(self) -> None:
        if not WATCHLIST_PATH.exists():
            log.error(f"找不到 {WATCHLIST_PATH}")
            self._watchlist = {"stocks": [], "market_open": "09:00", "market_close": "13:30", "shutdown_grace": "13:35"}
            return
        with self._lock:
            self._watchlist = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
            self._watchlist_mtime = WATCHLIST_PATH.stat().st_mtime

    def _load_thresholds(self) -> None:
        if not THRESHOLDS_PATH.exists():
            log.error(f"找不到 {THRESHOLDS_PATH}")
            self._thresholds = {}
            return
        with self._lock:
            self._thresholds = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
            self._thresholds_mtime = THRESHOLDS_PATH.stat().st_mtime

    def _save_watchlist(self) -> None:
        with self._lock:
            WATCHLIST_PATH.write_text(
                json.dumps(self._watchlist, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._watchlist_mtime = WATCHLIST_PATH.stat().st_mtime

    def _save_thresholds(self) -> None:
        with self._lock:
            THRESHOLDS_PATH.write_text(
                json.dumps(self._thresholds, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._thresholds_mtime = THRESHOLDS_PATH.stat().st_mtime

    def reload_if_changed(self) -> tuple[bool, bool]:
        """偵測 JSON 檔案是否被外部修改(人工編輯或 MCP 工具寫入),回傳 (watchlist_changed, thresholds_changed)。"""
        wl_changed = th_changed = False
        if WATCHLIST_PATH.exists() and WATCHLIST_PATH.stat().st_mtime != self._watchlist_mtime:
            self._load_watchlist()
            wl_changed = True
        if THRESHOLDS_PATH.exists() and THRESHOLDS_PATH.stat().st_mtime != self._thresholds_mtime:
            self._load_thresholds()
            th_changed = True
        return wl_changed, th_changed

    # ── Watchlist 讀取 ────────────────────────────────────────
    @property
    def stocks(self) -> list[dict]:
        with self._lock:
            return list(self._watchlist.get("stocks", []))

    @property
    def symbols(self) -> list[str]:
        return [s["symbol"] for s in self.stocks]

    def get_stock(self, symbol: str) -> Optional[dict]:
        for s in self.stocks:
            if s["symbol"] == symbol:
                return s
        return None

    @property
    def market_open(self) -> str:
        return self._watchlist.get("market_open", "09:00")

    @property
    def market_close(self) -> str:
        return self._watchlist.get("market_close", "13:30")

    @property
    def shutdown_grace(self) -> str:
        return self._watchlist.get("shutdown_grace", "13:35")

    @property
    def mode(self) -> str:
        return self._watchlist.get("mode", "paper")

    # ── Watchlist 寫入(供 MCP 控制工具用)─────────────────────
    def add_stock(self, symbol: str, name: str = "", cost: float = 0, shares: int = 0,
                  sector: str = "", notes: str = "") -> bool:
        with self._lock:
            if self.get_stock(symbol):
                return False
            self._watchlist.setdefault("stocks", []).append({
                "symbol": symbol, "name": name or symbol, "cost": cost,
                "shares": shares, "sector": sector, "notes": notes,
            })
            self._save_watchlist()
            return True

    def remove_stock(self, symbol: str) -> bool:
        with self._lock:
            stocks = self._watchlist.get("stocks", [])
            new_stocks = [s for s in stocks if s["symbol"] != symbol]
            if len(new_stocks) == len(stocks):
                return False
            self._watchlist["stocks"] = new_stocks
            self._save_watchlist()
            return True

    # ── Thresholds 讀取 ───────────────────────────────────────
    def get(self, dotted_path: str, default=None) -> Any:
        """通用點號路徑讀取,例如 config.get('strategy.R1.min_qty')。"""
        with self._lock:
            return _get(self._thresholds, dotted_path, default)

    def strategy_params(self, rule: str, symbol: Optional[str] = None) -> dict:
        """回傳某條規則的有效門檻。

        優先序:strategy_overrides.<symbol>.<rule> 逐欄覆蓋 strategy.<rule> 全域預設。
        override 只需列出想改的欄位,其餘沿用全域 → 高價股(台積)用全域,
        低價股(凱基)用自己的 override,兩檔都能正常觸發。
        symbol 省略時只回全域預設。
        """
        with self._lock:
            base = dict(_get(self._thresholds, f"strategy.{rule}", {}))
            if symbol:
                override = _get(self._thresholds, f"strategy_overrides.{symbol}.{rule}", None)
                if isinstance(override, dict):
                    base.update(override)
            return base

    def strategy_overrides(self, symbol: Optional[str] = None) -> dict:
        """讀取 per-symbol 門檻 override。symbol 省略回全部;指定則回該股(可能為空 dict)。"""
        with self._lock:
            allo = dict(_get(self._thresholds, "strategy_overrides", {}))
            return dict(allo.get(symbol, {})) if symbol else allo

    @property
    def auction_window(self) -> tuple[str, str]:
        return self.get("auction.start", "09:00:00"), self.get("auction.end", "09:00:30")

    @property
    def signal_cooldown_sec(self) -> int:
        return self.get("signal_cooldown_sec", 300)

    def price_monitor_config(self) -> dict:
        return dict(self.get("price_monitor", {"interval_sec": 30, "alert_ticks": 4}))

    def health_check_config(self) -> dict:
        return dict(self.get("health_check", {}))

    def llm_config(self) -> dict:
        return dict(self.get("llm", {}))

    def indicators_config(self) -> dict:
        return dict(self.get("indicators", {}))

    def logging_config(self) -> dict:
        return dict(self.get("logging", {"max_bytes": 10485760, "backup_count": 5}))

    # ── Thresholds 寫入(供 MCP 控制工具用)────────────────────
    def update(self, dotted_path: str, value: Any) -> None:
        """通用點號路徑寫入,例如 config.update('strategy.R1.min_qty', 60)。"""
        with self._lock:
            _set(self._thresholds, dotted_path, value)
            self._save_thresholds()

    def update_many(self, updates: dict[str, Any]) -> None:
        with self._lock:
            for path, value in updates.items():
                _set(self._thresholds, path, value)
            self._save_thresholds()

    def snapshot(self) -> dict:
        """回傳目前完整設定(watchlist + thresholds),給 MCP get_config 工具用。"""
        with self._lock:
            return {"watchlist": dict(self._watchlist), "thresholds": dict(self._thresholds)}


# 全域單例
config = ConfigStore()


# ===== .env 密鑰(靜態,不需熱重載)=====
SHIOAJI_API_KEY = os.getenv("SHIOAJI_API_KEY", "")
SHIOAJI_SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY", "")
SHIOAJI_PERSON_ID = os.getenv("SHIOAJI_PERSON_ID", "").split()[0] if os.getenv("SHIOAJI_PERSON_ID") else ""
SHIOAJI_SIMULATED = os.getenv("SHIOAJI_SIMULATED", "1") == "1"

# Claude Code Router(取代原 MiniMax 直連;CCR 在本機跑,統一路由到 Claude)
CCR_BASE_URL = os.getenv("CCR_BASE_URL", "http://127.0.0.1:3456")
CCR_API_KEY = os.getenv("CCR_API_KEY", "sk-ccr-local")
CCR_MODEL = os.getenv("CCR_MODEL", "claude-sonnet-4-5")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

EBOOK_DB_PATH = os.getenv("EBOOK_DB_PATH", str(ROOT.parent.parent / "ebook-library" / "db"))
