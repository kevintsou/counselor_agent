"""
軍師系統 — 價格監控 (core/price_monitor.py)
每 N 秒（config 驅動）對比一次各檔成交價，變動達 ≥ alert_ticks 檔才推 Telegram（不走 LLM）。

「N 檔」= alert_ticks × tick_size(prev_price),依 TWSE 升降單位計算。

baseline 機制:
  - 讀取即時成交價,與「上一次 alert 觸發時的價」比較
  - |diff| < 門檻 → 安靜(過濾雜訊,快照不變)
  - |diff| ≥ 門檻 → 推播 + 把現價寫成新基準
  - 第一次快照只記錄,不推播(沒有比較基準)
"""
import logging
from datetime import datetime

from config import config

log = logging.getLogger("counselor.price_monitor")


def tick_size(price: float) -> float:
    """台股升降單位（檔位）— 依 TWSE 規定。

    價格區間      每檔跳動
    ─────────────────────
    < 10          0.01
    10 – 50       0.05
    50 – 100      0.10
    100 – 500     0.50
    500 – 1000    1.00
    ≥ 1000        5.00
    """
    if price < 10:
        return 0.01
    elif price < 50:
        return 0.05
    elif price < 100:
        return 0.10
    elif price < 500:
        return 0.50
    elif price < 1000:
        return 1.00
    else:
        return 5.00


class PriceMonitor:
    def __init__(self):
        self._last_price: dict[str, float] = {}   # 即時成交價(on_tick 餵入)
        self._price_snap: dict[str, float] = {}    # 上次 alert 觸發時的基準價
        self._last_check_epoch: float = 0.0

    def update_price(self, symbol: str, price: float) -> None:
        if price > 0:
            self._last_price[symbol] = price

    def get_price(self, symbol: str) -> float | None:
        return self._last_price.get(symbol)

    def due(self, now_epoch: float) -> bool:
        interval = config.price_monitor_config().get("interval_sec", 30)
        return now_epoch - self._last_check_epoch >= interval

    def check(self, stocks: list[dict]) -> None:
        """對每檔比對即時價與基準價,達門檻則推播並更新基準。"""
        self._last_check_epoch = _now_epoch()
        now_str = datetime.now().strftime("%H:%M:%S")
        try:
            from notify.herald import send_price_alert
        except Exception as e:
            log.error(f"PriceMonitor herald import 失敗: {e}")
            return

        alert_ticks = config.price_monitor_config().get("alert_ticks", 4)

        for s in stocks:
            sym = s["symbol"]
            name = s.get("name", sym)
            curr = self._last_price.get(sym)
            if curr is None or curr <= 0:
                continue

            prev = self._price_snap.get(sym)
            if prev is None:
                self._price_snap[sym] = curr
                log.debug(f"  📸 PriceMonitor 首次快照 {sym} @ {curr}")
                continue

            diff = curr - prev
            threshold = alert_ticks * tick_size(prev)
            if abs(diff) < threshold - 1e-9:
                continue

            self._price_snap[sym] = curr  # 只有觸發 alert 才更新基準
            pct = diff / prev * 100
            icon = "📈" if diff > 0 else "📉"
            sign = "+" if diff > 0 else ""
            ticks_moved = round(abs(diff) / tick_size(prev))
            log.info(
                f"  {icon} PriceMonitor {sym} {prev} → {curr} "
                f"({sign}{pct:.2f}%  {ticks_moved} 檔  門檻={threshold})"
            )
            try:
                send_price_alert(sym, name, prev, curr, now_str, ticks_moved)
            except Exception as e:
                log.error(f"  ❌ PriceMonitor 推播失敗({sym}): {e}")

    def forget(self, symbol: str) -> None:
        """watchlist 熱重載移除個股時清掉狀態。"""
        self._last_price.pop(symbol, None)
        self._price_snap.pop(symbol, None)


def _now_epoch() -> float:
    import time
    return time.time()
