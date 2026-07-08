"""
軍師系統 — 偵查兵進入點 (sentinel.py)
============================
薄啟動腳本:設定 logging 後把控制權交給 core.sentinel.Sentinel。
所有偵測邏輯 / watchdog / PriceMonitor 邏輯都在 core/ 套件裡,方便單元測試與重用。
"""
import logging
import logging.handlers
import sys

from config import LOGS_DIR, config

log_cfg = config.logging_config()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOGS_DIR / "sentinel.log",
            maxBytes=log_cfg.get("max_bytes", 10 * 1024 * 1024),
            backupCount=log_cfg.get("backup_count", 5),
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("sentinel")

from core.sentinel import Sentinel  # noqa: E402  (要等 logging 設好才 import)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        s = Sentinel()
        for stock in s.stocks:
            print(f"  - {stock['symbol']} {stock['name']} 部位={stock['shares']}張")
        print("✅ watchlist 載入正常")
    elif len(sys.argv) > 1 and sys.argv[1] == "simulate":
        import random
        from datetime import datetime

        print("=== 模擬觸發測試 ===\n")
        s = Sentinel()
        sym = s.stocks[0]["symbol"] if s.stocks else "0000"
        now = datetime.now()

        print("情境 1: R1(短窗口內多筆大額買單)")
        for _ in range(6):
            r, detail = s.detector.feed(sym, now, random.randint(50, 80), "buy", 25.5)
            if r:
                print(f"  → 觸發: {r}, 筆數={detail.get(r, {}).get('count')}")

        print("\n情境 2: R2(較長窗口內更多大額買單)")
        for i in range(11):
            ts = datetime.fromtimestamp(now.timestamp() + i)
            r, detail = s.detector.feed(sym, ts, random.randint(100, 200), "buy", 25.5)
            if r:
                print(f"  → 觸發: {r}, 筆數={detail.get(r, {}).get('count')}")

        print("\n情境 3: R3(窗口內大幅淨買)")
        for i in range(5):
            ts = datetime.fromtimestamp(now.timestamp() + i * 5)
            r, detail = s.detector.feed(sym, ts, 5000, "buy", 25.5)
            if r:
                print(f"  → 觸發: {r}, 淨買={detail.get('R3', {}).get('net_lots')}")

        print("\n✅ 模擬測試完成")
    else:
        Sentinel().run()
