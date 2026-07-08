"""
軍師系統 — 觸發策略 (core/strategies.py)
純邏輯,不碰 Shioaji / Queue / IO,方便單元測試。

規則(所有數字都從 config/thresholds.json 讀,不寫死):
  R1 快攻:N 秒內 ≥ min_qty 張買單 ≥ min_count 筆
  R2 快攻:同 R1,窗口更長、門檻更高
  R3 淨買:窗口內 淨買張數 > amount_divisor / 成交價
  R4 計分:窗口內 大單買賣差分 counter > trigger_count
"""
import logging
import time as _time
from collections import deque
from datetime import datetime, time as dtime
from typing import Optional

from config import config

log = logging.getLogger("counselor.strategies")


def _parse_time(s: str) -> dtime:
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return dtime(*parts)


class StrategyDetector:
    """Kevin 自訂觸發策略 v2。所有門檻透過 config.strategy_params(rule) 即時讀取,支援熱重載。"""

    def __init__(self):
        # 每檔獨立 buffer: deque[(ts, qty, side, price)]
        self.buffers: dict[str, deque] = {}
        # 每檔最後一次 R3 觸發時間(epoch sec)
        self._last_r3_ts: dict[str, float] = {}
        # 每檔「(日期)已記錄開盤第一筆試撮」的旗標,跨日自動重置
        self._auction_consumed: set[tuple[str, str]] = set()

    def feed(self, symbol: str, ts: datetime, qty: int, side: str, price: float) -> tuple[str, dict]:
        """吃一筆 tick,回傳 (訊號等級, 觸發明細 dict)。

        訊號等級: '' / 'R1' / 'R2' / 'R3' / 'R4' / 'COMBO'
        任一觸發即回傳,並清空 buffer 避免重複觸發。
        """
        ts_epoch = ts.timestamp()

        # 過濾開盤試撮(只在設定的試撮時段,且是該檔當日第一筆)
        auction_start_s, auction_end_s = config.auction_window
        auction_start, auction_end = _parse_time(auction_start_s), _parse_time(auction_end_s)
        t = ts.time()
        in_auction = auction_start <= t < auction_end
        date_key = ts.strftime("%Y-%m-%d")
        auction_key = (symbol, date_key)
        if in_auction and auction_key not in self._auction_consumed:
            self._auction_consumed.add(auction_key)
            log.debug(f"  ⚠️  開盤試撮略過 {symbol} qty={qty} @ {ts}")
            return "", {}

        # 規格化 side(接受 'buy'/'sell' 或 1/2/-1 或 'Buy'/'Sell')
        s = str(side).lower()
        if s in ("buy", "1", "1.0", "true"):
            side_norm = "buy"
        elif s in ("sell", "2", "2.0", "-1", "-1.0", "false"):
            side_norm = "sell"
        else:
            side_norm = "unknown"

        buf = self.buffers.setdefault(symbol, deque())
        buf.append((ts_epoch, qty, side_norm, price))

        latest_price = price if price > 0 else 1.0

        r1p, r2p, r3p, r4p = (config.strategy_params(r) for r in ("R1", "R2", "R3", "R4"))

        r1_hit, r1_detail = self._check_window(buf, r1p["window_sec"], r1p["min_qty"], r1p["min_count"], return_detail=True)
        r2_hit, r2_detail = self._check_window(buf, r2p["window_sec"], r2p["min_qty"], r2p["min_count"], return_detail=True)
        r3_hit, r3_detail = self._check_r3(buf, latest_price, r3p, return_detail=True)
        r4_hit, r4_detail = self._check_r4(buf, r4p, return_detail=True)

        triggered = []
        detail_map: dict = {
            "rule": None,
            "triggered_at": ts.strftime("%H:%M:%S.%f")[:-3],
            "price": latest_price,
            "qty": qty,
            "side": side_norm,
            "thresholds": {"R1": r1p, "R2": r2p, "R3": r3p, "R4": r4p},
        }
        if r1_hit: triggered.append("R1"); detail_map["R1"] = r1_detail
        if r2_hit: triggered.append("R2"); detail_map["R2"] = r2_detail
        if r3_hit: triggered.append("R3"); detail_map["R3"] = r3_detail
        if r4_hit: triggered.append("R4"); detail_map["R4"] = r4_detail

        if not triggered:
            return "", {}

        # R3 冷卻:同 symbol cooldown_sec 內不重複觸發,避免同波大單連發
        if "R3" in triggered:
            cooldown_sec = r3p["cooldown_sec"]
            last_r3 = self._last_r3_ts.get(symbol, 0)
            if ts_epoch - last_r3 < cooldown_sec:
                log.debug(f"  ⏸️  R3 cooldown {symbol} 剩 {cooldown_sec - (ts_epoch-last_r3):.0f}s")
                triggered.remove("R3")
                detail_map.pop("R3", None)
            self._last_r3_ts[symbol] = ts_epoch
            if "R3" in triggered:
                d = detail_map["R3"]
                log.info(
                    f"  💰 R3 觸發 {symbol}:淨買 {d['net_lots']} 張 > 門檻 "
                    f"{d['threshold_lots']:.0f} 張(市值約 ${d['market_value_twd']:,.0f})"
                )

        if not triggered:
            return "", {}

        # 任一規則觸發後清空 buffer,避免同波數據被多個規則重複計算
        buf.clear()
        detail_map["rule"] = "+".join(triggered)
        if len(triggered) >= 2:
            log.info(f"🔴 COMBO 觸發 {symbol} {'+'.join(triggered)}")
            return "COMBO", detail_map
        log.info(f"🟡 {triggered[0]} 觸發 {symbol}")
        return triggered[0], detail_map

    @staticmethod
    def _check_window(buf: deque, window_sec: int, min_qty: int, min_count: int, return_detail: bool = False):
        """通用「N 秒內 ≥min_qty 張買單 ≥min_count 筆」檢查(R1/R2 共用形狀,門檻不同)。"""
        now = buf[-1][0]
        cutoff = now - window_sec
        big_buys = [b for b in buf if b[0] >= cutoff and b[2] == "buy" and b[1] >= min_qty]
        hit = len(big_buys) >= min_count
        if not return_detail:
            return hit
        qtys = [b[1] for b in big_buys]
        prices = [b[3] for b in big_buys]
        detail = {
            "window_sec": window_sec,
            "count": len(big_buys),
            "required_count": min_count,
            "total_lots": sum(qtys),
            "max_lot": max(qtys) if qtys else 0,
            "min_lot": min(qtys) if qtys else 0,
            "avg_lot": (sum(qtys) / len(qtys)) if qtys else 0,
            "price_high": max(prices) if prices else 0,
            "price_low": min(prices) if prices else 0,
            "ticks": [
                {"ts": datetime.fromtimestamp(b[0]).strftime("%H:%M:%S.%f")[:-3], "qty": b[1], "price": b[3]}
                for b in big_buys
            ],
        }
        return hit, detail

    @staticmethod
    def _check_r3(buf: deque, price: float, params: dict, return_detail: bool = False):
        """R3: window_sec 內淨買張數 > amount_divisor / 成交價(張數門檻)。"""
        if price <= 0:
            return (False, {}) if return_detail else False
        window_sec = params["window_sec"]
        now = buf[-1][0]
        cutoff = now - window_sec
        relevant = [b for b in buf if b[0] >= cutoff]
        buy_ticks = [b for b in relevant if b[2] == "buy"]
        sell_ticks = [b for b in relevant if b[2] == "sell"]
        buy_vol = sum(b[1] for b in buy_ticks)
        sell_vol = sum(b[1] for b in sell_ticks)
        net_qty = buy_vol - sell_vol
        threshold_lots = params["amount_divisor"] / price
        hit = net_qty > threshold_lots
        if not return_detail:
            return hit
        market_value = net_qty * 1000 * price
        detail = {
            "window_sec": window_sec,
            "threshold_lots": round(threshold_lots, 1),
            "buy_lots": buy_vol,
            "sell_lots": sell_vol,
            "net_lots": net_qty,
            "buy_sell_ratio": round(buy_vol / sell_vol, 2) if sell_vol > 0 else None,
            "market_value_twd": round(market_value, 0),
            "buy_ticks": [
                {"ts": datetime.fromtimestamp(b[0]).strftime("%H:%M:%S.%f")[:-3], "qty": b[1], "price": b[3]}
                for b in buy_ticks
            ],
            "sell_ticks": [
                {"ts": datetime.fromtimestamp(b[0]).strftime("%H:%M:%S.%f")[:-3], "qty": b[1], "price": b[3]}
                for b in sell_ticks
            ],
        }
        return hit, detail

    @staticmethod
    def _check_r4(buf: deque, params: dict, return_detail: bool = False):
        """R4: 累計計分制 — 單筆買盤 > min_qty → count++,單筆賣盤 > min_qty → count--,window 內 count > trigger_count 觸發。"""
        window_sec, min_qty, trigger_count = params["window_sec"], params["min_qty"], params["trigger_count"]
        now = buf[-1][0]
        cutoff = now - window_sec
        relevant = [b for b in buf if b[0] >= cutoff]
        count = 0
        buy_hits = 0
        sell_hits = 0
        for _, qty, side, _ in relevant:
            if side == "buy" and qty > min_qty:
                count += 1
                buy_hits += 1
            elif side == "sell" and qty > min_qty:
                count -= 1
                sell_hits += 1
        hit = count > trigger_count
        if hit:
            log.info(f"  🎯 R4 觸發:counter = {count}(買盤加 / 賣盤減 >{min_qty} 張單)")
        if not return_detail:
            return hit
        detail = {
            "window_sec": window_sec,
            "counter": count,
            "required_counter": trigger_count,
            "buy_hits": buy_hits,
            "sell_hits": sell_hits,
            "ticks": [
                {"ts": datetime.fromtimestamp(b[0]).strftime("%H:%M:%S.%f")[:-3], "qty": b[1], "side": b[2], "price": b[3]}
                for b in relevant if b[1] > min_qty
            ],
        }
        return hit, detail


class CooldownGate:
    """訊號冷卻:同 symbol+signal 在設定秒數內只放行一次。"""

    def __init__(self, seconds: Optional[int] = None):
        self.seconds = seconds if seconds is not None else config.signal_cooldown_sec
        self._last: dict[str, float] = {}

    def allow(self, symbol: str, signal: str) -> bool:
        key = f"{symbol}:{signal}"
        now = _time.time()
        if key in self._last and now - self._last[key] < self.seconds:
            return False
        self._last[key] = now
        return True
