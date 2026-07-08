"""
軍師系統 — 偵查兵 (core/sentinel.py)
角色:盤中即時監控 watchlist 個股,偵測進場觸發策略,派工給 strategist 子進程。

呼叫鏈:
   Shioaji TickSTKv1 → on_tick → StrategyDetector
   → CooldownGate → multiprocessing.Queue
   → strategist(子進程) → LLM(經 Claude Code Router)→ Telegram

核心原則:狀態全部收斂進 Sentinel 實例,不留模組級全域變數。
"""
import json
import logging
import multiprocessing
import signal
import time
from datetime import datetime, timedelta

from broker import broker
from config import STATE_DIR, config
from version import __version__

from .price_monitor import PriceMonitor
from .strategies import CooldownGate, StrategyDetector

log = logging.getLogger("counselor.sentinel")


def _parse_hhmm(s: str):
    from datetime import time as dtime
    h, m = s.split(":")[:2]
    return dtime(int(h), int(m))


class Sentinel:
    def __init__(self):
        self.stocks = config.stocks
        self._running = True

        # 觸發偵測
        self.detector = StrategyDetector()
        self.cooldown_gate = CooldownGate()
        self.price_monitor = PriceMonitor()

        # tick 流量狀態(watchdog 用)
        self._last_tick_ts: dict[str, datetime] = {}

        # 子進程通訊:multiprocessing.Queue 讓 strategist 有獨立 GIL,不與 Shioaji tokio runtime 搶鎖
        self.trigger_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=100)
        self._strategist_proc: multiprocessing.Process | None = None

        # Watchdog 狀態
        self._last_health_check: datetime | None = None
        self._consecutive_reconnect_fails: int = 0
        self._last_alert_ts: dict[str, datetime] = {}
        self._last_resub_ts: dict[str, float] = {}
        self._last_heartbeat: float = 0.0

        log.info(f"🧭 台股軍師 v{__version__} 啟動,監控 {len(self.stocks)} 檔")

    def stop(self, *_):
        log.info("🛑 收到停止訊號")
        self._running = False

    # ── 子進程生命週期 ────────────────────────────────────────
    def _spawn_strategist(self) -> multiprocessing.Process:
        p = multiprocessing.Process(
            target=_run_strategist_child,
            args=(self.trigger_queue,),
            name="strategist-child",
            daemon=False,
        )
        p.start()
        log.info(f"🧠 strategist 子進程啟動 PID={p.pid}")
        return p

    def _check_strategist_alive(self):
        if self._strategist_proc is None or not self._strategist_proc.is_alive():
            log.warning("⚠️ strategist 子進程已死,重啟中...")
            try:
                from notify.herald import send_alert
                send_alert("red", f"strategist 子進程重啟 @ {datetime.now().strftime('%H:%M:%S')}")
            except Exception:
                pass
            self._strategist_proc = self._spawn_strategist()

    # ── Shioaji Tick callback ────────────────────────────────
    def on_tick(self, exchange, tick):
        """Shioaji TickSTKv1 callback。只負責偵測 + 推 multiprocessing.Queue;
        LLM/Telegram 推播由獨立子進程 strategist 處理,進程隔離,絕不互搶 GIL。
        """
        try:
            symbol = str(tick.code)
            self._last_tick_ts[symbol] = datetime.now()
            qty = int(getattr(tick, "volume", 0) or 0)
            tick_type = getattr(tick, "tick_type", None)
            if tick_type in (1, "1", "Buy", 1.0):
                side = "buy"
            elif tick_type in (2, -1, "2", "-1", "Sell", 2.0, -1.0):
                side = "sell"
            else:
                side = "unknown"
            price = float(getattr(tick, "close", 0) or 0)
            self.price_monitor.update_price(symbol, price)
            ts = datetime.now()

            sig, detail = self.detector.feed(symbol, ts, qty, side, price)
            if sig:
                if not self.cooldown_gate.allow(symbol, sig):
                    log.info(f"  ⏸️ {symbol} {sig} 冷卻中,跳過推播")
                    return
                try:
                    self.trigger_queue.put_nowait({
                        "symbol": symbol, "sig": sig, "detail": detail,
                        "qty": qty, "side": side, "price": price, "ts": ts.isoformat(),
                    })
                    log.info(f"  📥 {symbol} {sig} 推入子進程 queue")
                except Exception as e:
                    log.error(f"  ❌ put queue 失敗({symbol} {sig} 丟棄): {e}")
        except Exception as e:
            log.error(f"on_tick 處理失敗: {e}")

    # ── Alert(冷卻式)──────────────────────────────────────
    def _alert(self, key: str, msg: str, cooldown_sec: int = 300):
        now = datetime.now()
        last = self._last_alert_ts.get(key)
        if last and (now - last).total_seconds() < cooldown_sec:
            return
        self._last_alert_ts[key] = now
        try:
            from notify.herald import send_alert
            send_alert("red", f"軍師 {msg}")
        except Exception as e:
            log.error(f"  ❌ alert 推播失敗: {e}")

    # ── Watchlist 熱重載(JSON,MCP 工具或人工編輯皆可觸發)────
    def _maybe_reload_config(self):
        wl_changed, th_changed = config.reload_if_changed()
        if th_changed:
            log.info("♻️ thresholds.json 已變更,策略/門檻即時套用")
        if not wl_changed:
            return
        new_stocks = config.stocks
        if not new_stocks:
            log.warning("⚠️ watchlist 重載後為空,忽略本次變更")
            return
        old_syms = {s["symbol"] for s in self.stocks}
        new_syms = {s["symbol"] for s in new_stocks}
        added = new_syms - old_syms
        removed = old_syms - new_syms
        for sym in added:
            if broker.subscribe_tick(sym, self.on_tick):
                log.info(f"  ➕ 熱重載新增訂閱 {sym}")
        for sym in removed:
            broker.unsubscribe_tick(sym)
            self._last_tick_ts.pop(sym, None)
            self.price_monitor.forget(sym)
            log.info(f"  ➖ 熱重載取消訂閱 {sym}")
        self.stocks = new_stocks
        if added or removed:
            log.info(f"♻️ watchlist 已重載(+{len(added)} / -{len(removed)}),現監控 {len(self.stocks)} 檔")

    # ── Watchdog ──────────────────────────────────────────
    def _do_health_check(self):
        """tick 流量被動偵測 + Shioaji session 狀態感知 + per-symbol 靜默重訂閱。"""
        now = datetime.now()
        hc = config.health_check_config()
        no_tick_alert_sec = hc.get("no_tick_alert_sec", 300)
        no_tick_resub_sec = hc.get("no_tick_resub_sec", 120)
        max_reconnect_fails = hc.get("max_reconnect_fails", 3)
        alert_cooldown = hc.get("alert_cooldown_sec", 300)
        reconnect_ok_cooldown = hc.get("reconnect_ok_cooldown_sec", 3600)

        self._maybe_reload_config()
        self._check_strategist_alive()

        sj_recovering = broker.session_recovering
        sj_down_age = broker.session_down_age()

        if sj_recovering:
            if sj_down_age < broker.session_recovery_grace:
                log.debug(
                    f"  ⏳ Shioaji session auto-recovering ({sj_down_age:.0f}s / "
                    f"grace={broker.session_recovery_grace}s),watchdog 讓開"
                )
                self._last_health_check = now
                return
            else:
                log.warning(f"⚠️ Shioaji session 斷線已 {sj_down_age:.0f}s,超過寬限期,啟動手動重連")

        # 收盤後不告警/不重連(ticks 自然停止是預期行為)
        market_is_open = now.time() <= _parse_hhmm(config.market_close)

        # 1. tick 流量檢查(含 per-symbol 靜默重訂閱)
        all_ticks_dead = True
        known_syms = [st["symbol"] for st in self.stocks]
        for s in self.stocks:
            sym = s["symbol"]
            last = self._last_tick_ts.get(sym)
            if last:
                idle = (now - last).total_seconds()
                if idle < 30:
                    all_ticks_dead = False
                if market_is_open and not sj_recovering:
                    if no_tick_resub_sec <= idle < no_tick_alert_sec:
                        last_resub = self._last_resub_ts.get(sym, 0)
                        if time.time() - last_resub >= no_tick_resub_sec:
                            log.warning(f"  🔄 {sym} 已 {int(idle)}s 沒 tick,靜默重訂閱")
                            broker.unsubscribe_tick(sym)
                            broker.subscribe_tick(sym, self.on_tick)
                            self._last_resub_ts[sym] = time.time()
                    elif idle >= no_tick_alert_sec:
                        self._alert(
                            f"no_tick_{sym}",
                            f"{sym} 已 {int(idle)}s 沒收到 tick,已嘗試重訂閱仍無效,請手動查證",
                            cooldown_sec=alert_cooldown,
                        )

        # 2. 手動重連判斷(只有 Shioaji 自動恢復失敗/從未連過 才到這裡)
        seen_before = any(s in self._last_tick_ts for s in known_syms)
        if market_is_open and all_ticks_dead and seen_before:
            log.warning("⚠️ 判斷 broker 斷線(Shioaji 自動恢復逾時),啟動手動重連")
            self._consecutive_reconnect_fails += 1
            resubs = [(s["symbol"], self.on_tick, "tick") for s in self.stocks]
            if broker.reconnect(retries=2, resubscribe_callbacks=resubs):
                log.info("✅ broker 手動重連成功")
                self._consecutive_reconnect_fails = 0
                self._alert("reconnect_ok", f"手動重連成功 @ {now.strftime('%H:%M:%S')}", cooldown_sec=reconnect_ok_cooldown)
            elif self._consecutive_reconnect_fails >= max_reconnect_fails:
                self._alert("broker_dead", f"⚠️ broker 連續 {max_reconnect_fails} 次手動重連失敗,軍師暫時失明", cooldown_sec=0)
        else:
            if self._consecutive_reconnect_fails > 0:
                log.info(f"✅ broker 恢復正常(曾失敗 {self._consecutive_reconnect_fails} 次)")
                self._consecutive_reconnect_fails = 0

        self._last_health_check = now

    def _heartbeat(self, label: str = ""):
        now_epoch = time.time()
        if now_epoch - self._last_heartbeat >= 60:
            suffix = f" ({label})" if label else ""
            log.info(f"♥ sentinel alive{suffix} {datetime.now().strftime('%m/%d %H:%M')}")
            self._last_heartbeat = now_epoch
            self._write_status(label)

    def _write_status(self, label: str = "") -> None:
        """把當前狀態寫進 state/sentinel_status.json,供 MCP get_sentinel_status 工具讀取。"""
        try:
            status = {
                "updated_at": datetime.now().isoformat(),
                "phase": label,
                "running": self._running,
                "watched_symbols": [s["symbol"] for s in self.stocks],
                "last_tick_ts": {sym: ts.isoformat() for sym, ts in self._last_tick_ts.items()},
                "strategist_alive": bool(self._strategist_proc and self._strategist_proc.is_alive()),
                "broker_connected": broker.is_alive(),
                "consecutive_reconnect_fails": self._consecutive_reconnect_fails,
            }
            (STATE_DIR / "sentinel_status.json").write_text(
                json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.debug(f"寫入 sentinel_status.json 失敗(不影響主流程): {e}")

    # ── 主迴圈 ────────────────────────────────────────────
    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        if not broker.connect():
            log.error("Shioaji 連線失敗,離開")
            try:
                from notify.herald import send_alert
                send_alert("red", "❌ sentinel 啟動失敗:Shioaji 連線失敗")
            except Exception:
                pass
            return

        self._strategist_proc = self._spawn_strategist()

        for s in self.stocks:
            if not broker.subscribe_tick(s["symbol"], self.on_tick):
                log.warning(f"  ⚠️ {s['symbol']} 訂閱失敗,跳過")

        hc = config.health_check_config()
        health_check_sec = hc.get("interval_sec", 10)
        log.info("📡 訂閱完成,進入主迴圈")
        log.info(f"  🩺 Watchdog:每 {health_check_sec}s health check")

        market_open = _parse_hhmm(config.market_open)
        shutdown_grace = _parse_hhmm(config.shutdown_grace)

        try:
            while self._running:
                # 週末休市:不告警不重連,只留心跳證明活著
                if datetime.now().weekday() >= 5:
                    self._heartbeat("週末休市")
                    time.sleep(60)
                    continue

                now = datetime.now().time()
                if now < market_open:
                    self._heartbeat("盤前")
                    time.sleep(30)
                    continue
                if now > shutdown_grace:
                    log.info("📭 收盤,睡到明天開盤(lazy sentinel 不退)")
                    now_dt = datetime.now()
                    tomorrow = (now_dt + timedelta(days=1)).replace(hour=8, minute=55, second=0, microsecond=0)
                    wait_sec = (tomorrow - now_dt).total_seconds()
                    log.info(f"  睡 {wait_sec/3600:.1f} 小時(到明天 08:55 暖機)")
                    slept = 0.0
                    while slept < wait_sec and self._running:
                        target = min(60, wait_sec - slept)
                        time.sleep(target)
                        slept += target
                        self._heartbeat("收盤休息")
                    if not self._running:
                        break
                    continue

                # 開盤中:每 1s 醒一次
                now_epoch = time.time()
                self._heartbeat("開盤中")
                if self._last_health_check is None or \
                   (datetime.now() - self._last_health_check).total_seconds() >= health_check_sec:
                    self._do_health_check()
                if self.price_monitor.due(now_epoch):
                    self.price_monitor.check(self.stocks)
                time.sleep(1)
        finally:
            broker.disconnect()


def _run_strategist_child(q: multiprocessing.Queue):
    """子進程進入點:呼叫 strategist.run_forever"""
    import strategist
    strategist.run_forever(q)
