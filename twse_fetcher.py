"""
軍師系統 — TWSE 公開資料抓取 (twse_fetcher.py) v6.1
全部改用 openapi.twse.com.tw(免費、免登入、CORS 友善)

端點:
  - 個股三大法人:❌ 個股免費無 → 改用 BFI82U 全市場
  - 個股融資融券:MI_MARGN(個股 + 增減都有了,MI_MARGN 內含昨日餘額)
  - 個股開高低收:STOCK_DAY_ALL(順便備用)
"""
import logging
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Optional
import requests

_ROOT = Path(__file__).parent
DB_PATH = _ROOT / "state" / "twse.db"

log = logging.getLogger("counselor.twse")

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 20
BASE = "https://openapi.twse.com.tw/v1"


def _ensure_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS institutional_3instit (
            trade_date TEXT NOT NULL,
            party TEXT NOT NULL,                -- 自營商/投信/外資及陸資
            buy_amount INTEGER DEFAULT 0,
            sell_amount INTEGER DEFAULT 0,
            net_amount INTEGER DEFAULT 0,
            PRIMARY KEY (trade_date, party)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS margin_short (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            margin_balance INTEGER DEFAULT 0,
            margin_change INTEGER DEFAULT 0,
            short_balance INTEGER DEFAULT 0,
            short_change INTEGER DEFAULT 0,
            PRIMARY KEY (trade_date, symbol)
        )
    """)
    conn.commit()
    return conn


def _to_int(s) -> int:
    return int(str(s).replace(",", "").strip() or 0)


def fetch_institutional_3instit(trade_date: Optional[str] = None) -> Optional[dict]:
    """
    抓三大法人當日買賣金額(全市場總額,不是個股)。
    端點:BFI82U(每日 18:00 後出 當日三大法人買賣金額)
    回傳 dict: {外資及陸資, 投信, 自營商, 合計}
    """
    if trade_date is None:
        trade_date = date.today().isoformat()
    # openapi BFI82U 無日期參數(就是「今天」的當下)
    for attempt in range(1, 4):
        try:
            log.info(f"📥 抓 BFI82U 三大法人(全市場) {trade_date}")
            r = requests.get(f"{BASE}/fund/BFI82U", headers=HEADERS, timeout=TIMEOUT)
            r.encoding = "utf-8"
            data = r.json()
            if not isinstance(data, list) or len(data) == 0:
                log.warning(f"  BFI82U 回空: {r.text[:200]}")
                time.sleep(1)
                continue
            result = {}
            conn = _ensure_db()
            for row in data:
                party = row.get("單位名稱", "").strip()
                buy = _to_int(row.get("買進金額", 0))
                sell = _to_int(row.get("賣出金額", 0))
                net = _to_int(row.get("買賣差額", 0))
                result[party] = {"buy": buy, "sell": sell, "net": net}
                conn.execute("""
                    INSERT OR REPLACE INTO institutional_3instit
                    (trade_date, party, buy_amount, sell_amount, net_amount)
                    VALUES (?, ?, ?, ?, ?)
                """, (trade_date, party, buy, sell, net))
            conn.commit()
            conn.close()
            log.info(f"   抓到 {len(result)} 個法人")
            for p, d in result.items():
                log.info(f"   {p}: 買 {d['buy']:,} 賣 {d['sell']:,} 淨 {d['net']:+,}")
            return result
        except Exception as e:
            log.warning(f"  抓取失敗(第 {attempt} 次): {e}")
            time.sleep(2)
    log.error("❌ BFI82U 抓取失敗")
    return None


def fetch_margin_short(symbol: str = "2883", trade_date: Optional[str] = None) -> Optional[dict]:
    """
    抓個股融資融券(全市場一次打包,過濾單檔)。
    端點:exchangeReport/MI_MARGN
    """
    if trade_date is None:
        trade_date = date.today().isoformat()
    for attempt in range(1, 4):
        try:
            log.info(f"📥 抓 MI_MARGN 融資券 {symbol} {trade_date}")
            r = requests.get(f"{BASE}/exchangeReport/MI_MARGN", headers=HEADERS, timeout=TIMEOUT)
            r.encoding = "utf-8"
            data = r.json()
            if not isinstance(data, list) or len(data) == 0:
                log.warning(f"  MI_MARGN 回空")
                time.sleep(1)
                continue
            for row in data:
                if str(row.get("股票代號", "")).strip() == symbol:
                    margin_balance = _to_int(row.get("融資今日餘額", 0))
                    margin_yesterday = _to_int(row.get("融資前日餘額", 0))
                    short_balance = _to_int(row.get("融券今日餘額", 0))
                    short_yesterday = _to_int(row.get("融券前日餘額", 0))
                    result = {
                        "symbol": symbol,
                        "trade_date": trade_date,
                        "margin_balance": margin_balance,
                        "margin_change": margin_balance - margin_yesterday,
                        "short_balance": short_balance,
                        "short_change": short_balance - short_yesterday,
                    }
                    result["short_margin_ratio"] = (
                        result["short_balance"] / result["margin_balance"] * 100
                        if result["margin_balance"] > 0 else 0
                    )
                    conn = _ensure_db()
                    conn.execute("""
                        INSERT OR REPLACE INTO margin_short
                        (trade_date, symbol, margin_balance, margin_change, short_balance, short_change)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (trade_date, symbol,
                          result["margin_balance"], result["margin_change"],
                          result["short_balance"], result["short_change"]))
                    conn.commit()
                    conn.close()
                    log.info(f"   融資 {margin_balance:,}({result['margin_change']:+,}) "
                             f"融券 {short_balance:,}({result['short_change']:+,}) "
                             f"券資 {result['short_margin_ratio']:.2f}%")
                    return result
            log.warning(f"   {symbol} 不在 MI_MARGN 表")
            return None
        except Exception as e:
            log.warning(f"  抓取失敗(第 {attempt} 次): {e}")
            time.sleep(2)
    log.error("❌ MI_MARGN 抓取失敗")
    return None


def load_institutional(trade_date: str) -> dict:
    """讀回當日三大法人(全市場)dict。"""
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM institutional_3instit WHERE trade_date=?",
        (trade_date,),
    )
    return {r["party"]: dict(r) for r in cur.fetchall()}


def load_margin_short(symbol: str, trade_date: str) -> Optional[dict]:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM margin_short WHERE symbol=? AND trade_date=?",
        (symbol, trade_date),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    print("=== 三大法人(全市場) ===")
    print(fetch_institutional_3instit(target))
    print("\n=== 融資券(2883) ===")
    print(fetch_margin_short("2883", target))
