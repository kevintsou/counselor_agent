"""
軍師系統 — 大盤指數抓取 (market_index_fetcher.py)
改用 TWSE openapi FMTQIK(免費、免登入、穩定)

端點:
  - FMTQIK:每日市場成交資訊(含加權指數 開/收/漲跌)
  - STOCK_DAY_ALL:個股日成交(可順便驗證 2883 收盤)
"""
import logging
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Optional
import requests

_ROOT = Path(__file__).parent
DB_PATH = _ROOT / "state" / "market.db"

log = logging.getLogger("counselor.market")

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 20
BASE = "https://openapi.twse.com.tw/v1"


def _ensure_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_index (
            trade_date TEXT PRIMARY KEY,
            taiex_close REAL,
            taiex_change REAL,
            taiex_volume INTEGER,
            taiex_trade_value INTEGER,
            transactions INTEGER
        )
    """)
    conn.commit()
    return conn


def _to_int(s) -> int:
    return int(str(s).replace(",", "").strip() or 0)


def _to_float(s) -> float:
    return float(str(s).replace(",", "").strip() or 0)


def fetch_market_index(trade_date: Optional[str] = None) -> Optional[dict]:
    """
    抓當日加權指數收盤 + 漲跌 + 總成交量。
    FMTQIK 回傳近 3 日(今 + 前 2 交易日)。
    """
    if trade_date is None:
        trade_date = date.today().isoformat()
    # 轉民國日期給過濾
    y, m, d = trade_date.split("-")
    roc_date = f"{int(y) - 1911}{m}{d}"
    for attempt in range(1, 4):
        try:
            log.info(f"📥 抓 FMTQIK 大盤 {trade_date}")
            r = requests.get(f"{BASE}/exchangeReport/FMTQIK", headers=HEADERS, timeout=TIMEOUT)
            r.encoding = "utf-8"
            data = r.json()
            if not isinstance(data, list) or len(data) == 0:
                log.warning(f"  FMTQIK 回空")
                time.sleep(1)
                continue
            for row in data:
                if row.get("Date", "").strip() == roc_date:
                    result = {
                        "trade_date": trade_date,
                        "taiex_close": _to_float(row.get("TAIEX", 0)),
                        "taiex_change": _to_float(row.get("Change", 0)),
                        "taiex_volume": _to_int(row.get("TradeVolume", 0)),
                        "taiex_trade_value": _to_int(row.get("TradeValue", 0)),
                        "transactions": _to_int(row.get("Transaction", 0)),
                    }
                    conn = _ensure_db()
                    conn.execute("""
                        INSERT OR REPLACE INTO market_index
                        (trade_date, taiex_close, taiex_change, taiex_volume, taiex_trade_value, transactions)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (result["trade_date"], result["taiex_close"],
                          result["taiex_change"], result["taiex_volume"],
                          result["taiex_trade_value"], result["transactions"]))
                    conn.commit()
                    conn.close()
                    log.info(f"   加權 {result['taiex_close']} {result['taiex_change']:+,} "
                             f"量 {result['taiex_volume']:,}張 額 {result['taiex_trade_value']:,}")
                    return result
            log.warning(f"   {trade_date}({roc_date}) 不在 FMTQIK")
            return None
        except Exception as e:
            log.warning(f"  抓取失敗(第 {attempt} 次): {e}")
            time.sleep(2)
    log.error("❌ FMTQIK 抓取失敗")
    return None


def load_market(trade_date: str) -> Optional[dict]:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM market_index WHERE trade_date=?", (trade_date,)
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    print(fetch_market_index(target))
