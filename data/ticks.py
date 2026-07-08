"""
軍師系統 — Ticks 抓取模組 (data/ticks.py)
盤後從 Shioaji 抓當天全日 ticks + 收盤五檔,落 sqlite。

設計原則:
- 盤後獨立運作,不依賴 sentinel
- 內建 timeout / 重試
- 失敗不 panic,回傳 None 讓上層判斷
- 標的一律由呼叫端傳入,不寫死代號
"""
import logging
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Optional

from config import STATE_DIR

log = logging.getLogger("counselor.data.ticks")

DB_PATH = STATE_DIR / "ticks.db"


def _ensure_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticks (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            ts INTEGER NOT NULL,
            close REAL NOT NULL,
            volume INTEGER NOT NULL,
            tick_type INTEGER NOT NULL,
            PRIMARY KEY (symbol, trade_date, ts)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume INTEGER, amount INTEGER,
            bid_price_1 REAL, bid_qty_1 INTEGER,
            ask_price_1 REAL, ask_qty_1 INTEGER,
            bid_total_5 INTEGER, ask_total_5 INTEGER,
            ts INTEGER,
            PRIMARY KEY (symbol, trade_date)
        )
    """)
    conn.commit()
    return conn


def fetch_ticks(symbol: str, trade_date: Optional[str] = None) -> Optional[dict]:
    """抓當天全日 ticks + 收盤五檔,落 sqlite。

    Args:
        symbol: 股票代號(必填,不設預設值)
        trade_date: YYYY-MM-DD,None = 今日
    Returns:
        dict with keys: tick_count, snapshot, db_path,或 None(失敗)
    """
    if trade_date is None:
        trade_date = date.today().isoformat()

    from broker import broker

    if not broker.connect(retries=2):
        log.error("❌ Shioaji 連線失敗,無法抓 ticks")
        return None

    try:
        contract = broker.get_contract(symbol)
        if not contract:
            log.error(f"❌ 找不到合約 {symbol}")
            return None

        log.info(f"📥 抓 {symbol} {trade_date} ticks ...")
        t0 = time.time()
        raw = broker._api.ticks(contract, date=trade_date)
        rd = raw.dict()
        n = len(rd.get("ts", []))
        log.info(f"   抓到 {n} 筆 ticks(耗時 {time.time()-t0:.1f}s)")

        if n == 0:
            log.warning(f"⚠️ {symbol} {trade_date} 沒有 ticks(可能非交易日)")
            return None

        conn = _ensure_db()
        rows = list(zip(
            [symbol] * n, [trade_date] * n,
            rd["ts"], rd["close"], rd["volume"], rd["tick_type"],
        ))
        conn.executemany("INSERT OR REPLACE INTO ticks VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        log.info(f"   寫入 {n} 筆 ticks 到 {DB_PATH.name}")

        log.info(f"📸 抓 {symbol} 收盤快照 ...")
        snap = broker._api.snapshots([contract])
        if not snap:
            log.warning("⚠️ snapshots() 回傳空")
            return {"tick_count": n, "snapshot": None, "db_path": str(DB_PATH)}

        s = snap[0]
        bid_total_5 = ask_total_5 = 0
        for i in range(1, 6):
            bid_total_5 += int(getattr(s, f"bid_qty_{i}", None) or 0)
            ask_total_5 += int(getattr(s, f"ask_qty_{i}", None) or 0)

        snap_data = {
            "symbol": symbol,
            "trade_date": trade_date,
            "open": float(getattr(s, "open", 0) or 0),
            "high": float(getattr(s, "high", 0) or 0),
            "low": float(getattr(s, "low", 0) or 0),
            "close": float(getattr(s, "close", 0) or 0),
            "volume": int(getattr(s, "volume", 0) or 0),
            "amount": int(getattr(s, "amount", 0) or 0),
            "bid_price_1": float(getattr(s, "bid_price_1", 0) or 0),
            "bid_qty_1": int(getattr(s, "bid_qty_1", 0) or 0),
            "ask_price_1": float(getattr(s, "ask_price_1", 0) or 0),
            "ask_qty_1": int(getattr(s, "ask_qty_1", 0) or 0),
            "bid_total_5": bid_total_5,
            "ask_total_5": ask_total_5,
            "ts": int(time.time() * 1000),
        }
        conn.execute("""
            INSERT OR REPLACE INTO snapshots VALUES (
                :symbol, :trade_date, :open, :high, :low, :close,
                :volume, :amount, :bid_price_1, :bid_qty_1,
                :ask_price_1, :ask_qty_1, :bid_total_5, :ask_total_5, :ts
            )
        """, snap_data)
        conn.commit()
        conn.close()

        log.info(f"   快照落檔: 開 {snap_data['open']} 高 {snap_data['high']} "
                 f"低 {snap_data['low']} 收 {snap_data['close']} 量 {snap_data['volume']:,}")

        return {"tick_count": n, "snapshot": snap_data, "db_path": str(DB_PATH)}

    except Exception as e:
        log.error(f"❌ fetch_ticks 例外: {e}", exc_info=True)
        return None
    finally:
        broker.disconnect(timeout=10)


def load_ticks_from_db(symbol: str, trade_date: str) -> list[dict]:
    """從 sqlite 讀回 ticks(給 data/indicators.py 用)。"""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM ticks WHERE symbol=? AND trade_date=? ORDER BY ts",
        (symbol, trade_date),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def load_snapshot_from_db(symbol: str, trade_date: str) -> Optional[dict]:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM snapshots WHERE symbol=? AND trade_date=?",
        (symbol, trade_date),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


if __name__ == "__main__":
    import sys
    from config import config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    target_date = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()
    target_symbol = sys.argv[1] if len(sys.argv) > 1 else (config.symbols[0] if config.symbols else None)
    if not target_symbol:
        print("用法: python -m data.ticks <symbol> [date]")
        sys.exit(1)
    result = fetch_ticks(target_symbol, target_date)
    if result:
        print(f"\n✅ 完成:ticks={result['tick_count']} 快照={'有' if result.get('snapshot') else '無'}")
    else:
        print("\n❌ 抓取失敗")
