"""
軍師系統 — 偵查兵 (sentinel.py)
============================
角色：盤中即時監控 watchlist 個股,偵測進場觸發策略

觸發策略:
  R1 快攻:5 秒內 ≥ 50 張買單 ≥ 5 筆
  R2 快攻:30 秒內 ≥ 100 張買單 ≥ 10 筆
  R3 淨買:60 秒內 淨買張數 > 100000 / 成交價(約 1 億市值)
  R4 計分:60 秒內 大單買賣差分 counter > 20

呼叫鏈:
   Shioaji TickSTKv1 → on_tick → StrategyDetector
   → CooldownGate → multiprocessing.Queue
   → strategist(子進程) → LLM → Telegram
"""
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, time as dtime
from pathlib import Path

import multiprocessing

import yaml
from dotenv import load_dotenv

# 載入 .env
load_dotenv(Path(__file__).parent / ".env")

# 注意:LLM(llm_client)/Telegram(herald)由 strategist 子進程處理,
# 父進程只需 broker;send_alert 在用到的地方各自 local import。
from broker import broker
from version import __version__

# ===== 設定 =====
ROOT = Path(__file__).parent
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)
WATCHLIST = ROOT / "watchlist.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOGS / "sentinel.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("sentinel")

# 盤中時段
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(13, 30)
SHUTDOWN_GRACE = dtime(13, 35)


# ===== Watchlist =====
def load_watchlist() -> list[dict]:
    if not WATCHLIST.exists():
        log.error(f"找不到 watchlist.yaml: {WATCHLIST}")
        return []
    data = yaml.safe_load(WATCHLIST.read_text())
    return data.get("stocks", [])


# ===== 觸發策略 v2 (2026-06-03 重寫) =====
class StrategyDetector:
    """Kevin 自訂觸發策略 v2。

    規則:
      R1 快攻:5秒內 50張↑買單 ≥ 5 筆
      R2 快攻:30秒內 100張買單 ≥ 10 筆
      R3 淨買:60秒內 買-賣 > (1000*25)/成交價 (25 萬市值)
    """

    # R1 參數
    R1_WINDOW_SEC = 5
    R1_MIN_QTY = 50
    R1_MIN_COUNT = 5
    # R2 參數
    R2_WINDOW_SEC = 30
    R2_MIN_QTY = 100
    R2_MIN_COUNT = 10
    # R3 參數
    R3_WINDOW_SEC = 60
    R3_AMOUNT_DIVISOR = 100000     # 門檻公式: 100000 / 成交價 = 張數門檻 (約 1 億市值)
    # R4 參數
    R4_MIN_QTY = 50                # 單筆 > 50 張才算
    R4_TRIGGER_COUNT = 20          # counter 累積 / 扣減 超過 20 觸發
    # 試撮過濾(2026-06-03 修正,只過濾「開盤試撮時段」)
    # Kevin 口述:「試撮會在 9 點開盤第一筆成交,後面的都是真正的單筆成交單」
    # 台股試撮只發生在 9:00:00 ~ 9:00:30(集合競價)→ 只在這 30 秒內標記每檔第一筆
    AUCTION_START = dtime(9, 0)
    AUCTION_END = dtime(9, 0, 30)
    R3_COOLDOWN_SEC = 60          # R3 觸發後冷卻 1 分鐘(同一波大單不重複觸發)

    def __init__(self):
        # 每檔獨立 buffer: deque[(ts, qty, side, price)]
        self.buffers: dict[str, deque] = {}
        # 每檔最後一次 R3 觸發時間(epoch sec)
        self._last_r3_ts: dict[str, float] = {}
        # 每檔「(日期)已記錄開盤第一筆試撮」的旗標
        # key: (symbol, date_str),跨日自動重置
        self._auction_consumed: set[tuple[str, str]] = set()

    def feed(self, symbol: str, ts: datetime, qty: int, side: str, price: float) -> tuple[str, dict]:
        """吃一筆 tick,回傳 (訊號等級, 觸發明細 dict)。

        訊號等級: '' / 'R1' / 'R2' / 'R3' / 'R4' / 'COMBO'
        明細 dict 包含每個觸發規則的完整數據(R1/R2/R3/R4 各自的窗口、計數、張數、市值、逐筆明細)。
        任一觸發即回傳,並清空 buffer 避免重複觸發。
        """
        ts_epoch = ts.timestamp()
        # 過濾 1:開盤試撮(只在 9:00:00~9:00:30 試撮時段,且是該檔當日第一筆)
        t = ts.time()
        in_auction = self.AUCTION_START <= t < self.AUCTION_END
        date_key = ts.strftime("%Y-%m-%d")
        auction_key = (symbol, date_key)
        if in_auction and auction_key not in self._auction_consumed:
            self._auction_consumed.add(auction_key)
            log.debug(f"  ⚠️  開盤試撮略過 {symbol} qty={qty} @ {ts}")
            return "", {}

        # 規格化 side(接受 'buy'/'sell' 或 1/2/-1 或 'Buy'/'Sell')
        # 無法判定一律維持 'unknown',R1~R4 只計入明確買/賣,避免污染淨買量與 counter
        s = str(side).lower()
        if s in ("buy", "1", "1.0", "true"):
            side_norm = "buy"
        elif s in ("sell", "2", "2.0", "-1", "-1.0", "false"):
            side_norm = "sell"
        else:
            side_norm = "unknown"

        buf = self.buffers.setdefault(symbol, deque())
        buf.append((ts_epoch, qty, side_norm, price))

        # 抓最新成交價
        latest_price = price if price > 0 else 1.0

        # === R1: 5秒內 50張↑買單 ≥ 5 筆 ===
        r1_hit, r1_detail = self._check_r1(buf, return_detail=True)

        # === R2: 30秒內 100張買單 ≥ 10 筆 ===
        r2_hit, r2_detail = self._check_r2(buf, return_detail=True)

        # === R3: 60秒內 淨買 > 25 萬市值 ===
        r3_hit, r3_detail = self._check_r3(buf, latest_price, return_detail=True)

        # === R4: 累計計分(60秒內 +20)===
        r4_hit, r4_detail = self._check_r4(buf, return_detail=True)

        triggered = []
        detail_map: dict = {
            "rule": None,
            "triggered_at": ts.strftime("%H:%M:%S.%f")[:-3],
            "price": latest_price,
            "qty": qty,
            "side": side_norm,
            "thresholds": {
                "R1": {"window_sec": self.R1_WINDOW_SEC, "min_qty": self.R1_MIN_QTY, "min_count": self.R1_MIN_COUNT},
                "R2": {"window_sec": self.R2_WINDOW_SEC, "min_qty": self.R2_MIN_QTY, "min_count": self.R2_MIN_COUNT},
                "R3": {"window_sec": self.R3_WINDOW_SEC, "amount_divisor": self.R3_AMOUNT_DIVISOR, "cooldown_sec": self.R3_COOLDOWN_SEC},
                "R4": {"min_qty": self.R4_MIN_QTY, "trigger_count": self.R4_TRIGGER_COUNT, "window_sec": self.R3_WINDOW_SEC},
            },
        }
        if r1_hit: triggered.append("R1"); detail_map["R1"] = r1_detail
        if r2_hit: triggered.append("R2"); detail_map["R2"] = r2_detail
        if r3_hit: triggered.append("R3"); detail_map["R3"] = r3_detail
        if r4_hit: triggered.append("R4"); detail_map["R4"] = r4_detail

        if triggered:
            # R3 冷卻:同 symbol COOLDOWN 秒內不重複觸發,避免同波大單連發
            # 不論成功放行或被擋下,都推進 last_r3_ts:
            #   成功 → 記錄本次時間,下次需再隔 COOLDOWN 秒
            #   擋下 → 持續延後,防止同波大單在冷卻期邊界反覆放行
            if "R3" in triggered:
                last_r3 = self._last_r3_ts.get(symbol, 0)
                if ts_epoch - last_r3 < self.R3_COOLDOWN_SEC:
                    log.debug(f"  ⏸️  R3 cooldown {symbol} 剩 {self.R3_COOLDOWN_SEC - (ts_epoch-last_r3):.0f}s")
                    triggered.remove("R3")
                    detail_map.pop("R3", None)
                self._last_r3_ts[symbol] = ts_epoch
                if "R3" in triggered:
                    d = detail_map["R3"]
                    log.info(
                        f"  💰 R3 觸發 {symbol}:淨買 {d['net_lots']} 張 > 門檻 "
                        f"{d['threshold_lots']:.0f} 張 (市值約 ${d['market_value_twd']:,.0f})"
                    )
            # 任一規則觸發後清空 buffer,避免同波數據被多個規則重複計算
            if triggered:
                buf.clear()
            if not triggered:
                return "", {}
            detail_map["rule"] = "+".join(triggered)
            if len(triggered) >= 2:
                log.info(f"🔴 COMBO 觸發 {symbol} {'+'.join(triggered)}")
                return "COMBO", detail_map
            log.info(f"🟡 {triggered[0]} 觸發 {symbol}")
            return triggered[0], detail_map
        return "", {}

    def _check_r1(self, buf: deque, return_detail: bool = False):
        """R1: 5秒內 50張↑買單 ≥ 5 筆

        回傳: (hit: bool, detail: dict)
        detail 包含: window 內買單筆數 / 總張數 / 最大單筆 / 最小單筆 / 平均張數 / 逐筆明細
        """
        now = buf[-1][0]
        cutoff = now - self.R1_WINDOW_SEC
        big_buys = [b for b in buf if b[0] >= cutoff and b[2] == "buy" and b[1] >= self.R1_MIN_QTY]
        hit = len(big_buys) >= self.R1_MIN_COUNT
        if not return_detail:
            return hit
        qtys = [b[1] for b in big_buys]
        prices = [b[3] for b in big_buys]
        detail = {
            "window_sec": self.R1_WINDOW_SEC,
            "count": len(big_buys),
            "required_count": self.R1_MIN_COUNT,
            "total_lots": sum(qtys),
            "max_lot": max(qtys) if qtys else 0,
            "min_lot": min(qtys) if qtys else 0,
            "avg_lot": (sum(qtys) / len(qtys)) if qtys else 0,
            "price_high": max(prices) if prices else 0,
            "price_low": min(prices) if prices else 0,
            "ticks": [
                {"ts": datetime.fromtimestamp(b[0]).strftime("%H:%M:%S.%f")[:-3],
                 "qty": b[1], "price": b[3]}
                for b in big_buys
            ],
        }
        return hit, detail

    def _check_r2(self, buf: deque, return_detail: bool = False):
        """R2: 30秒內 100張買單 ≥ 10 筆

        回傳: (hit, detail)
        """
        now = buf[-1][0]
        cutoff = now - self.R2_WINDOW_SEC
        big_buys = [b for b in buf if b[0] >= cutoff and b[2] == "buy" and b[1] >= self.R2_MIN_QTY]
        hit = len(big_buys) >= self.R2_MIN_COUNT
        if not return_detail:
            return hit
        qtys = [b[1] for b in big_buys]
        prices = [b[3] for b in big_buys]
        detail = {
            "window_sec": self.R2_WINDOW_SEC,
            "count": len(big_buys),
            "required_count": self.R2_MIN_COUNT,
            "total_lots": sum(qtys),
            "max_lot": max(qtys) if qtys else 0,
            "min_lot": min(qtys) if qtys else 0,
            "avg_lot": (sum(qtys) / len(qtys)) if qtys else 0,
            "price_high": max(prices) if prices else 0,
            "price_low": min(prices) if prices else 0,
            "ticks": [
                {"ts": datetime.fromtimestamp(b[0]).strftime("%H:%M:%S.%f")[:-3],
                 "qty": b[1], "price": b[3]}
                for b in big_buys
            ],
        }
        return hit, detail

    def _check_r3(self, buf: deque, price: float, return_detail: bool = False):
        """R3: 60秒內 淨買張數 > 100000 / 成交價 (張數門檻)

        Kevin 定義:
          門檻張數 = 100000 / 成交價 (單位:張 = 1000股)
          @ 25.5 元 → 門檻 = 3,922 張 ≈ 1 億市值

        回傳: (hit, detail)
        detail 拆出 buy_vol / sell_vol / net / 買賣比 / 雙向逐筆
        """
        if price <= 0:
            return (False, {}) if return_detail else False
        now = buf[-1][0]
        cutoff = now - self.R3_WINDOW_SEC
        relevant = [b for b in buf if b[0] >= cutoff]
        buy_ticks = [b for b in relevant if b[2] == "buy"]
        sell_ticks = [b for b in relevant if b[2] == "sell"]
        buy_vol = sum(b[1] for b in buy_ticks)    # 張
        sell_vol = sum(b[1] for b in sell_ticks)  # 張
        net_qty = buy_vol - sell_vol  # 淨買(張)
        threshold_lots = self.R3_AMOUNT_DIVISOR / price  # 門檻張數
        hit = net_qty > threshold_lots
        if not return_detail:
            return hit
        market_value = net_qty * 1000 * price
        detail = {
            "window_sec": self.R3_WINDOW_SEC,
            "threshold_lots": round(threshold_lots, 1),
            "buy_lots": buy_vol,
            "sell_lots": sell_vol,
            "net_lots": net_qty,
            "buy_sell_ratio": round(buy_vol / sell_vol, 2) if sell_vol > 0 else None,
            "market_value_twd": round(market_value, 0),
            "buy_ticks": [
                {"ts": datetime.fromtimestamp(b[0]).strftime("%H:%M:%S.%f")[:-3],
                 "qty": b[1], "price": b[3]}
                for b in buy_ticks
            ],
            "sell_ticks": [
                {"ts": datetime.fromtimestamp(b[0]).strftime("%H:%M:%S.%f")[:-3],
                 "qty": b[1], "price": b[3]}
                for b in sell_ticks
            ],
        }
        return hit, detail

    def _check_r4(self, buf: deque, return_detail: bool = False):
        """R4: 累計計分制

        - 單筆買盤 > 50 張 → count++
        - 單筆賣盤 > 50 張 → count--
        - 累積到 60 秒 window 內
        - count > 20 → 觸發

        (用 R3 同一個 60 秒 window)

        回傳: (hit, detail)
        """
        now = buf[-1][0]
        cutoff = now - self.R3_WINDOW_SEC
        relevant = [b for b in buf if b[0] >= cutoff]
        count = 0
        buy_hits = 0
        sell_hits = 0
        for _, qty, side, _ in relevant:
            if side == "buy" and qty > self.R4_MIN_QTY:
                count += 1
                buy_hits += 1
            elif side == "sell" and qty > self.R4_MIN_QTY:
                count -= 1
                sell_hits += 1
        hit = count > self.R4_TRIGGER_COUNT
        if not return_detail:
            if hit:
                log.info(f"  🎯 R4 觸發:counter = {count} (買盤加 / 賣盤減 >50 張單)")
            return hit
        detail = {
            "window_sec": self.R3_WINDOW_SEC,
            "counter": count,
            "required_counter": self.R4_TRIGGER_COUNT,
            "buy_hits": buy_hits,
            "sell_hits": sell_hits,
            "ticks": [
                {"ts": datetime.fromtimestamp(b[0]).strftime("%H:%M:%S.%f")[:-3],
                 "qty": b[1], "side": b[2], "price": b[3]}
                for b in relevant
                if b[1] > self.R4_MIN_QTY
            ],
        }
        if hit:
            log.info(f"  🎯 R4 觸發:counter = {count} (買盤加 / 賣盤減 >50 張單)")
        return hit, detail


# 全域 detector 實例
detector = StrategyDetector()

# Watchdog 全域狀態(module level,on_tick 可直接寫)
_LAST_TICK_TS: dict[str, datetime] = {}
# 每檔最新成交價(由 on_tick 維護,供 PriceMonitor 讀取)
_LAST_PRICE: dict[str, float] = {}
# multiprocessing.Queue:strategist 子進程有獨立 GIL,不會與 Shioaji tokio runtime 搶鎖
_TRIGGER_QUEUE: multiprocessing.Queue = multiprocessing.Queue(maxsize=100)
_STRATEGIST_PROC: multiprocessing.Process | None = None


def _spawn_strategist() -> multiprocessing.Process:
    """啟動 strategist 子進程(只在 sentinel 主程序內呼叫一次)。"""
    p = multiprocessing.Process(
        target=_run_strategist_child,
        args=(_TRIGGER_QUEUE,),
        name="strategist-child",
        daemon=False,  # 非 daemon,讓子進程能完整處理完最後一則 task 才退出
    )
    p.start()
    log.info(f"🧠 strategist 子進程啟動 PID={p.pid}")
    return p


def _run_strategist_child(q: multiprocessing.Queue):
    """子進程進入點:呼叫 strategist.run_forever"""
    import strategist
    strategist.run_forever(q)


def _check_strategist_alive():
    """檢查子進程是否還活,死了就重啟。"""
    global _STRATEGIST_PROC
    if _STRATEGIST_PROC is None or not _STRATEGIST_PROC.is_alive():
        log.warning("⚠️ strategist 子進程已死,重啟中...")
        try:
            from herald import send_alert
            send_alert("red", f"strategist 子進程重啟 @ {datetime.now().strftime('%H:%M:%S')}")
        except Exception:
            pass
        _STRATEGIST_PROC = _spawn_strategist()


# ===== Shioaji Tick callback =====
def on_tick(exchange, tick):
    """Shioaji TickSTKv1 callback。

    Shioaji 1.5+ 正式簽名:set_on_tick_stk_v1_callback → (exchange, tick)
    exchange: sj.Exchange  (TSE / OTC,通常不用)
    tick:     sj.TickSTKv1 (.code / .close / .volume / .tick_type ...)

    v7 修正(2026-06-04):
      - 只負責偵測 + 推 multiprocessing.Queue
      - LLM/Telegram 推播由獨立子進程 strategist.py 處理
      - 進程隔離,絕不互搶 GIL
    """
    try:
        symbol = str(tick.code)
        # 記錄最後 tick 時間與成交價(供 watchdog / PriceMonitor 讀取)
        _LAST_TICK_TS[symbol] = datetime.now()
        qty = int(getattr(tick, "volume", 0) or 0)
        # Shioaji TickSTKv1.tick_type 區分買賣主動方:
        #   0=無法判定 / 1=外盤(買方主動) / 2=內盤(賣方主動)
        #   (相容舊版/字串/-1 寫法)
        tick_type = getattr(tick, "tick_type", None)
        if tick_type in (1, "1", "Buy", 1.0):
            side = "buy"
        elif tick_type in (2, -1, "2", "-1", "Sell", 2.0, -1.0):
            side = "sell"
        else:
            side = "unknown"  # 0/None/其他 → 不歸類買賣
        price = float(getattr(tick, "close", 0) or 0)
        if price > 0:
            _LAST_PRICE[symbol] = price   # PriceMonitor 用
        ts = datetime.now()

        # 偵測觸發(回傳 (signal, detail) tuple)
        sig, detail = detector.feed(symbol, ts, qty, side, price)
        if sig:
            # 訊號冷卻:同 symbol+signal 300s 內只推一次,避免同波觸發洗版
            if not _COOLDOWN_GATE.allow(symbol, sig):
                log.info(f"  ⏸️ {symbol} {sig} 冷卻中,跳過推播")
                return
            # 推到子進程 queue(非阻塞,Queue 滿就丟棄,不會拉 shioaji 後腿)
            try:
                _TRIGGER_QUEUE.put_nowait({
                    "symbol": symbol, "sig": sig, "detail": detail,
                    "qty": qty, "side": side, "price": price, "ts": ts.isoformat(),
                })
                log.info(f"  📥 {symbol} {sig} 推入子進程 queue")
            except Exception as e:
                log.error(f"  ❌ put queue 失敗({symbol} {sig} 丟棄): {e}")
    except Exception as e:
        log.error(f"on_tick 處理失敗: {e}")


# ===== 訊號冷卻 =====
class CooldownGate:
    def __init__(self, seconds: int = 300):
        self.seconds = seconds
        self._last: dict[str, float] = {}

    def allow(self, symbol: str, signal: str) -> bool:
        key = f"{symbol}:{signal}"
        now = time.time()
        if key in self._last and now - self._last[key] < self.seconds:
            return False
        self._last[key] = now
        return True


# 全域訊號冷卻閘門:同 symbol+signal 在 N 秒內只推播一次
# (在 on_tick 推 queue 前過濾,避免同一波觸發狂洗 LLM/Telegram)
_COOLDOWN_GATE = CooldownGate(seconds=300)


# ===== 主迴圈 =====
class Sentinel:
    # Watchdog 設定
    HEALTH_CHECK_SEC    = 10    # 每 10s 跑一次 health check
    NO_TICK_ALERT_SEC   = 60    # 60s 沒 tick 就告警
    MAX_RECONNECT_FAILS = 3     # 連續重連失敗 N 次發 alert
    # PriceMonitor 設定
    PRICE_MONITOR_SEC   = 30    # 每 30s 對比一次成交價

    def __init__(self):
        self.stocks = load_watchlist()
        self._running = True
        # watchlist 熱重載
        self._watchlist_mtime = WATCHLIST.stat().st_mtime if WATCHLIST.exists() else 0.0
        # Watchdog 狀態
        self._last_health_check: datetime | None = None
        self._consecutive_reconnect_fails: int = 0
        self._last_alert_ts: dict[str, datetime] = {}
        # PriceMonitor 狀態
        self._price_snap: dict[str, float] = {}   # 上次快照的成交價 {symbol: price}
        self._last_price_check: float = 0.0        # 上次快照的 epoch time

        # ✅ Kevin 2026-06-08 拍板:不從 watchlist 的 cost 拿初始快照
        # 理由:cost 是進場成本價,不是前日收盤,用來當 baseline 語意不對
        # 改成:price_snap 留空,等第一筆 tick 來時在 _do_price_monitor 內設定(不推播)
        # 之後只在 alert 觸發時才更新(2026-06-08 baseline 機制)
        log.info(f"  📸 PriceMonitor 初始快照:留空,等第一筆 tick 來時記錄")

        log.info(f"🧭 台股軍師 v{__version__} 啟動,監控 {len(self.stocks)} 檔")

    def stop(self, *_):
        log.info("🛑 收到停止訊號")
        self._running = False

    def _alert(self, key: str, msg: str, cooldown_sec: int = 300):
        """發 alert 給 Kevin(同 key 5 分鐘內不重複推播)。"""
        now = datetime.now()
        last = self._last_alert_ts.get(key)
        if last and (now - last).total_seconds() < cooldown_sec:
            return  # 冷卻中
        self._last_alert_ts[key] = now
        try:
            from herald import send_alert
            send_alert("red", f"軍師 {msg}")
        except Exception as e:
            log.error(f"  ❌ alert 推播失敗: {e}")

    def _maybe_reload_watchlist(self):
        """偵測 watchlist.yaml 變更並熱重載(新增→訂閱 / 移除→取消訂閱)。

        watchlist.yaml 開頭聲稱「編輯後自動重載不需重啟」,此方法兌現該承諾。
        """
        try:
            if not WATCHLIST.exists():
                return
            mtime = WATCHLIST.stat().st_mtime
            if mtime == self._watchlist_mtime:
                return
            new_stocks = load_watchlist()
            if not new_stocks:
                log.warning("⚠️ watchlist 重載後為空,忽略本次變更")
                return
            old_syms = {s["symbol"] for s in self.stocks}
            new_syms = {s["symbol"] for s in new_stocks}
            added = new_syms - old_syms
            removed = old_syms - new_syms
            for sym in added:
                if broker.subscribe_tick(sym, on_tick):
                    log.info(f"  ➕ 熱重載新增訂閱 {sym}")
            for sym in removed:
                broker.unsubscribe_tick(sym)
                _LAST_TICK_TS.pop(sym, None)
                _LAST_PRICE.pop(sym, None)
                self._price_snap.pop(sym, None)
                log.info(f"  ➖ 熱重載取消訂閱 {sym}")
            self.stocks = new_stocks
            self._watchlist_mtime = mtime
            if added or removed:
                log.info(f"♻️ watchlist 已重載(+{len(added)} / -{len(removed)}),現監控 {len(self.stocks)} 檔")
        except Exception as e:
            log.error(f"  ❌ watchlist 重載失敗: {e}")

    @staticmethod
    def _tick_size(price: float) -> float:
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

    def _do_price_monitor(self):
        """每 30 秒對比一次各檔成交價，變動達 ≥ 4 檔才推 Telegram（不走 LLM）。

        「4 檔」= 4 × tick_size(prev_price)，依 TWSE 升降單位計算。
        例：凱基金 @ 25.5 → tick=0.05 → 門檻=0.20
            台積電 @ 920  → tick=1.00 → 門檻=4.00

        邏輯：
          - 讀取 _LAST_PRICE（on_tick 即時更新）
          - 與 _price_snap（**上一次 alert 觸發時的價**）比較
          - |diff| < 4 × tick_size → 安靜（過濾雜訊,快照不變)
          - |diff| ≥ 4 × tick_size → 漲 📈 / 跌 📉 推 Telegram + **把現價寫成新基準**
          - 第一次快照只記錄，不推播（沒有比較基準)
          - **2026-06-08 拍板:快照只有在 alert 觸發時才更新,沒觸發則沿用「上次 alert 的價」**
        """
        now_str = datetime.now().strftime("%H:%M:%S")
        try:
            from herald import send_price_alert
        except Exception as e:
            log.error(f"PriceMonitor herald import 失敗: {e}")
            return

        for s in self.stocks:
            sym  = s["symbol"]
            name = s.get("name", sym)
            curr = _LAST_PRICE.get(sym)

            if curr is None or curr <= 0:
                continue   # 還沒收到任何 tick

            prev = self._price_snap.get(sym)
            if prev is None:
                # 首次快照：只記錄，不推播
                self._price_snap[sym] = curr
                log.debug(f"  📸 PriceMonitor 首次快照 {sym} @ {curr}")
                continue

            diff      = curr - prev
            threshold = 4 * self._tick_size(prev)

            if abs(diff) < threshold - 1e-9:
                continue   # 變動不到 4 檔，不通知,快照不更新(沿用上次 alert 的價)

            # ✅ Kevin 2026-06-08 拍板:只有觸發 alert 才更新基準
            # 沒觸發 → 快照保持不變,下次仍以「上次 alert 的價」為基準
            self._price_snap[sym] = curr

            pct  = diff / prev * 100
            icon = "📈" if diff > 0 else "📉"
            sign = "+" if diff > 0 else ""
            ticks_moved = round(abs(diff) / self._tick_size(prev))
            log.info(
                f"  {icon} PriceMonitor {sym} {prev} → {curr} "
                f"({sign}{pct:.2f}%  {ticks_moved} 檔  門檻={threshold})"
            )
            try:
                send_price_alert(sym, name, prev, curr, now_str, ticks_moved)
            except Exception as e:
                log.error(f"  ❌ PriceMonitor 推播失敗({sym}): {e}")

    def _do_health_check(self):
        """Watchdog 主體(v8):tick 流量被動偵測 + Shioaji session 狀態感知。

        v8 重點修正(2026-06-05):
          Shioaji 底層走 Solace TCP,連線偶爾被 server 重置 (Connection reset by peer)。
          Shioaji SDK 內建最多 50 次自動重連,通常幾秒內自行恢復。
          過去 watchdog 不知道 Shioaji 正在自動恢復,會衝進來做 logout→login,
          兩個重連機制互打架,同時炸出大量 Telegram 告警。

          修正策略:
          1. broker._on_session_event 追蹤 Shioaji session 狀態
          2. session 正在自動恢復(< 3 分鐘)→ watchdog 完全讓開,不做任何動作
          3. session 恢復失敗超過 3 分鐘 → 才啟動手動 reconnect(真正的備援)
          4. 「no tick」告警 → 自動恢復期間靜音(避免假警報)
          5. 「重連成功」Telegram → 只發一次/小時(原本 5 分鐘,太吵)
        """
        now = datetime.now()

        # 0a. watchlist 熱重載
        self._maybe_reload_watchlist()

        # 0b. 檢查 strategist 子進程
        _check_strategist_alive()

        # 0c. 讀取 Shioaji session 狀態
        sj_recovering = broker.session_recovering
        sj_down_age   = broker.session_down_age()

        if sj_recovering:
            if sj_down_age < broker.SESSION_RECOVERY_GRACE:
                # Shioaji 自動恢復中且在寬限期內 → 完全讓開
                log.debug(
                    f"  ⏳ Shioaji session auto-recovering ({sj_down_age:.0f}s / "
                    f"grace={broker.SESSION_RECOVERY_GRACE}s),watchdog 讓開"
                )
                self._last_health_check = now
                return
            else:
                # 超過寬限期,Shioaji 自動恢復疑似失敗 → 繼續往下做手動重連
                log.warning(
                    f"⚠️ Shioaji session 斷線已 {sj_down_age:.0f}s,超過寬限期,啟動手動重連"
                )

        # 1. tick 流量檢查
        all_ticks_dead  = True
        known_syms      = [st["symbol"] for st in self.stocks]
        for s in self.stocks:
            sym  = s["symbol"]
            last = _LAST_TICK_TS.get(sym)
            if last:
                idle = (now - last).total_seconds()
                if idle < 30:
                    all_ticks_dead  = False
                if idle > self.NO_TICK_ALERT_SEC and not sj_recovering:
                    # 只在確認「不是 Shioaji 自動恢復」時才告警,避免假訊號
                    self._alert(
                        f"no_tick_{sym}",
                        f"{sym} 已 {int(idle)}s 沒收到 tick,請手動查證",
                        cooldown_sec=300,
                    )

        # 2. 手動重連判斷(只有 Shioaji 自動恢復失敗/從未連過 才到這裡)
        seen_before = any(s in _LAST_TICK_TS for s in known_syms)
        if all_ticks_dead and seen_before:
            log.warning("⚠️ 判斷 broker 斷線(Shioaji 自動恢復逾時),啟動手動重連")
            self._consecutive_reconnect_fails += 1
            resubs = [(s["symbol"], on_tick, "tick") for s in self.stocks]
            if broker.reconnect(retries=2, resubscribe_callbacks=resubs):
                log.info("✅ broker 手動重連成功")
                self._consecutive_reconnect_fails = 0
                # 重連成功 alert:1 小時內只發一次,不炸版
                self._alert(
                    "reconnect_ok",
                    f"手動重連成功 @ {now.strftime('%H:%M:%S')}",
                    cooldown_sec=3600,
                )
            elif self._consecutive_reconnect_fails >= self.MAX_RECONNECT_FAILS:
                self._alert(
                    "broker_dead",
                    f"⚠️ broker 連續 {self.MAX_RECONNECT_FAILS} 次手動重連失敗,軍師暫時失明",
                    cooldown_sec=0,
                )
        else:
            if self._consecutive_reconnect_fails > 0:
                log.info(f"✅ broker 恢復正常(曾失敗 {self._consecutive_reconnect_fails} 次)")
                self._consecutive_reconnect_fails = 0

        self._last_health_check = now

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        if not broker.connect():
            log.error("Shioaji 連線失敗,離開")
            try:
                from herald import send_alert
                send_alert("red", "❌ sentinel 啟動失敗:Shioaji 連線失敗")
            except Exception:
                pass  # herald 還沒載入也別讓 sentinel 死
            return

        global _STRATEGIST_PROC
        _STRATEGIST_PROC = _spawn_strategist()

        for s in self.stocks:
            if not broker.subscribe_tick(s["symbol"], on_tick):
                log.warning(f"  ⚠️ {s['symbol']} 訂閱失敗,跳過")

        log.info("📡 訂閱完成,進入主迴圈(開盤 9:00 ~ 收盤 13:30)")
        log.info(f"  🩺 Watchdog:每 {self.HEALTH_CHECK_SEC}s health check / {self.NO_TICK_ALERT_SEC}s 無 tick 告警")

        try:
            while self._running:
                now = datetime.now().time()
                if now < MARKET_OPEN:
                    time.sleep(30)
                    continue
                if now > SHUTDOWN_GRACE:
                    # v7.0.1 修:收盤後不是 break 退出,是 sleep 到明天開盤
                    # 避免 launchd KeepAlive 重複拉起(造成 -9 signal 堆積)
                    log.info("📭 收盤,睡到明天開盤(laze sentinel 不退)")
                    # 算到明天 8:55 開盤前 5 分鐘還有幾秒
                    from datetime import timedelta as _td
                    now_dt = datetime.now()
                    tomorrow = (now_dt + _td(days=1)).replace(hour=8, minute=55, second=0, microsecond=0)
                    wait_sec = (tomorrow - now_dt).total_seconds()
                    log.info(f"  睡 {wait_sec/3600:.1f} 小時(到明天 08:55 暖機)")
                    # 分段睡,讓訊號能中斷
                    # 修法:用 time.sleep 實際返回的剩餘時間,累加實睡(原本寫死 +60 會早醒)
                    slept = 0.0
                    while slept < wait_sec and self._running:
                        target = min(60, wait_sec - slept)
                        time.sleep(target)
                        slept += target  # 已睡滿(被中斷也是這樣算,保證不會錯過開盤)
                    if not self._running:
                        break
                    continue
                # 開盤中:每 1s 醒一次
                now_epoch = time.time()
                # health check (每 10s)
                if self._last_health_check is None or \
                   (datetime.now() - self._last_health_check).total_seconds() >= self.HEALTH_CHECK_SEC:
                    self._do_health_check()
                # price monitor (每 30s)
                if now_epoch - self._last_price_check >= self.PRICE_MONITOR_SEC:
                    self._do_price_monitor()
                    self._last_price_check = now_epoch
                time.sleep(1)
        finally:
            broker.disconnect()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        s = Sentinel()
        for stock in s.stocks:
            print(f"  - {stock['symbol']} {stock['name']} 部位={stock['shares']}張")
        print("✅ watchlist 載入正常")
    elif len(sys.argv) > 1 and sys.argv[1] == "simulate":
        # 模擬觸發測試(不連 Shioaji,直接餵假 tick)
        print("=== 模擬觸發測試 ===\n")
        import random
        sym = "2883"
        now = datetime.now()

        # 情境 1: 5秒內 5 筆 50+ 買單 → R1
        print("情境 1: R1 (5秒內 5 筆 50張↑ 買單)")
        for i in range(6):
            ts = now
            r, detail = detector.feed(sym, ts, random.randint(50, 80), "buy", 25.5)
            if r: print(f"  → 觸發: {r}, 筆數={detail.get(r, {}).get('count')}")

        # 情境 2: 30秒內 10 筆 100+ 買單 → R2
        print("\n情境 2: R2 (30秒內 10 筆 100張 買單)")
        for i in range(11):
            ts = datetime.fromtimestamp(now.timestamp() + i)
            r, detail = detector.feed(sym, ts, random.randint(100, 200), "buy", 25.5)
            if r: print(f"  → 觸發: {r}, 筆數={detail.get(r, {}).get('count')}")

        # 情境 3: 60秒內淨買 > 25 萬 → R3
        # 25萬 / 25.5 = 9804 張,買單要 >= 賣單 + 9804
        print("\n情境 3: R3 (60秒內淨買 > 25 萬市值)")
        for i in range(5):
            ts = datetime.fromtimestamp(now.timestamp() + i * 5)
            r, detail = detector.feed(sym, ts, 5000, "buy", 25.5)  # 單筆 5000 張買
            if r: print(f"  → 觸發: {r}, 淨買={detail.get('R3', {}).get('net_lots')}")

        print("\n✅ 模擬測試完成")
    elif len(sys.argv) > 1 and sys.argv[1] == "subscribe":
        log.info("📡 訂閱測試模式(10 秒後自動結束)")

        def stop_after():
            time.sleep(10)
            log.info("10 秒到,離開")
            os._exit(0)

        import threading
        threading.Thread(target=stop_after, daemon=True).start()

        if not broker.connect():
            sys.exit(1)
        for s in load_watchlist():
            broker.subscribe_tick(s["symbol"], on_tick)
        time.sleep(15)
        broker.disconnect()
    else:
        Sentinel().run()
