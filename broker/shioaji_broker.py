"""
軍師系統 — 券商連線 (broker/shioaji_broker.py)
封裝 Shioaji: 登入 / 訂閱 Tick / 抓 K 線 / 取快照。

設計原則:
- 模擬環境優先 (simulation=True),真實帳號需明確 SHIOAJI_SIMULATED=0
- Tick 走 callback,不阻塞主迴圈
- 內建 timeout / 重連(次數與寬限秒數均由 config/thresholds.json 的 health_check 區塊控制)
"""
import logging
import signal
import time
from typing import Optional, Callable

from config import SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY, SHIOAJI_SIMULATED, config

log = logging.getLogger("counselor.broker")


class Broker:
    # Shioaji Solace session event codes (from official docs)
    _EV_SESSION_UP = 0
    _EV_SESSION_DOWN = 1
    _EV_RECONNECTING = 12
    _EV_RECONNECTED = 13
    _EV_SUBSCRIBE_OK = 16

    def __init__(self):
        self._api = None
        self._connected = False
        self._session_recovering: bool = False
        self._session_down_ts: float = 0.0

    @property
    def session_recovery_grace(self) -> float:
        """手動重連前最多信任 Shioaji 自行恢復的秒數(config 驅動,預設 180s)。"""
        return config.health_check_config().get("session_recovery_grace_sec", 180)

    # ── Session event callback ──────────────────────────────────────────────
    def _on_session_event(self, resp_code: int, event_code: int, info: str, event: str):
        """Shioaji Solace session event callback。

        Shioaji 內建重連機制(最多 50 次),不需要我們介入。
        只做狀態追蹤,讓 sentinel watchdog 知道「正在自動恢復,不要衝進來打架」。
        """
        if event_code == self._EV_SUBSCRIBE_OK:
            return  # 太吵,不 log

        log.info(f"📡 Shioaji session [{event_code}] {event}")

        if event_code in (self._EV_SESSION_DOWN, self._EV_RECONNECTING):
            if not self._session_recovering:
                self._session_down_ts = time.time()
                self._session_recovering = True

        elif event_code in (self._EV_SESSION_UP, self._EV_RECONNECTED):
            was_recovering = self._session_recovering
            age = time.time() - self._session_down_ts if self._session_down_ts else 0
            self._session_recovering = False
            self._session_down_ts = 0.0
            self._connected = True
            if was_recovering:
                log.info(f"✅ Shioaji session 自動恢復完成(停機 {age:.0f}s)")

    @property
    def session_recovering(self) -> bool:
        return self._session_recovering

    def session_down_age(self) -> float:
        if not self._session_recovering or self._session_down_ts == 0:
            return 0.0
        return time.time() - self._session_down_ts

    # ── Connect / Disconnect ────────────────────────────────────────────────
    def connect(self, retries: int = 3) -> bool:
        """登入 Shioaji,失敗自動重試。"""
        if not all([SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY]):
            log.error("❌ Shioaji key 缺失,檢查 .env")
            return False
        for attempt in range(1, retries + 1):
            try:
                import shioaji as sj
                self._api = sj.Shioaji(simulation=SHIOAJI_SIMULATED)
                self._api.login(
                    api_key=SHIOAJI_API_KEY,
                    secret_key=SHIOAJI_SECRET_KEY,
                    contracts_timeout=3000,
                )
                try:
                    self._api.quote.set_event_callback(self._on_session_event)
                    log.debug("📡 session event callback 已掛載")
                except Exception as e:
                    log.warning(f"⚠️ session event callback 掛載失敗(可忽略): {e}")
                self._connected = True
                self._session_recovering = False
                self._session_down_ts = 0.0
                accts = self._api.list_accounts()
                log.info(f"✅ Shioaji 登入成功({['真實','模擬'][SHIOAJI_SIMULATED]}) {len(accts)} 帳戶")
                return True
            except Exception as e:
                log.warning(f"Shioaji 登入失敗(第 {attempt}/{retries} 次): {e}")
                self._api = None
                self._connected = False
                time.sleep(2)
        log.error(f"❌ Shioaji 連線失敗,重試 {retries} 次後放棄")
        self._api = None
        self._connected = False
        return False

    def disconnect(self, timeout: int = 10):
        """登出 Shioaji,加了 timeout 保護避免斷線狀態下 logout() 永久卡住。"""
        if not (self._api and self._connected):
            log.debug("disconnect() 被調用但未連線,跳過")
            return

        def _timeout_handler(signum, frame):
            raise TimeoutError("Shioaji logout 逾時")

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout)
        try:
            self._api.logout()
            log.info(f"📴 Shioaji 已登出(< {timeout}s 內完成)")
        except TimeoutError:
            log.warning(f"⚠️ Shioaji logout 逾時({timeout}s),強制標記為已斷線")
        except Exception as e:
            log.warning(f"⚠️ Shioaji logout 例外({e}),強制標記為已斷線")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            self._connected = False
            self._api = None

    def is_alive(self) -> bool:
        """健康檢查:斷線偵測改由「訂閱 callback 是否有在收到 tick」判斷,這裡只看 flag。"""
        return self._connected

    def reconnect(self, retries: int = 3, resubscribe_callbacks: Optional[list] = None) -> bool:
        """重連流程:logout → login → 重新訂閱。

        Args:
            retries: 最大重試次數
            resubscribe_callbacks: [(symbol, callback, quote_type), ...] 重新訂閱清單
        """
        log.warning("🔄 broker 開始重連程序...")
        try:
            if self._api and self._connected:
                self._api.logout()
        except Exception as e:
            log.debug(f"  舊 logout 例外(可忽略): {e}")
        self._connected = False
        self._api = None

        for attempt in range(1, retries + 1):
            try:
                log.info(f"  重連第 {attempt}/{retries} 次...")
                if self.connect(retries=1):
                    if resubscribe_callbacks:
                        for symbol, callback, quote_type in resubscribe_callbacks:
                            ok = self._resubscribe_one(symbol, callback, quote_type)
                            log.info(f"  重新訂閱 {symbol} ({quote_type}): {'✅' if ok else '❌'}")
                    log.info(f"✅ 重連成功(第 {attempt} 次)")
                    return True
            except Exception as e:
                log.warning(f"  重連第 {attempt} 次失敗: {e}")
            time.sleep(2 ** attempt)
        log.error(f"❌ broker 重連失敗,已重試 {retries} 次")
        return False

    def _resubscribe_one(self, symbol: str, callback: Callable, quote_type: str = "tick") -> bool:
        try:
            contract = self.get_contract(symbol)
            if not contract:
                return False
            if quote_type == "tick":
                self._api.set_on_tick_stk_v1_callback(callback)
            elif quote_type == "bidask":
                self._api.set_on_bidask_stk_v1_callback(callback)
            self._api.subscribe(contract, quote_type=quote_type)
            return True
        except Exception as e:
            log.error(f"  重訂閱 {symbol} 失敗: {e}")
            return False

    def get_contract(self, symbol: str):
        if not self._connected:
            return None
        try:
            return self._api.Contracts.Stocks[symbol]
        except KeyError:
            log.error(f"❌ 找不到股票代號: {symbol}")
            return None

    def subscribe_tick(self, symbol: str, callback: Callable) -> bool:
        if not self._connected:
            log.error("尚未連線,請先 connect()")
            return False
        try:
            contract = self.get_contract(symbol)
            if not contract:
                return False
            self._api.set_on_tick_stk_v1_callback(callback)
            self._api.subscribe(contract, quote_type="tick")
            log.info(f"📡 已訂閱 {symbol} tick")
            return True
        except Exception as e:
            log.error(f"訂閱 tick 失敗 ({symbol}): {e}")
            return False

    def unsubscribe_tick(self, symbol: str) -> bool:
        if not self._connected:
            return False
        try:
            contract = self.get_contract(symbol)
            if not contract:
                return False
            self._api.unsubscribe(contract, quote_type="tick")
            log.info(f"📴 已取消訂閱 {symbol} tick")
            return True
        except Exception as e:
            log.error(f"取消訂閱 tick 失敗 ({symbol}): {e}")
            return False

    def subscribe_bidask(self, symbol: str, callback: Callable) -> bool:
        if not self._connected:
            return False
        try:
            contract = self.get_contract(symbol)
            if not contract:
                return False
            self._api.set_on_bidask_stk_v1_callback(callback)
            self._api.subscribe(contract, quote_type="bidask")
            log.info(f"📡 已訂閱 {symbol} bidask")
            return True
        except Exception as e:
            log.error(f"訂閱 bidask 失敗 ({symbol}): {e}")
            return False

    def get_snapshot(self, symbol: str) -> Optional[dict]:
        """取得當前快照(價格/量/五檔)。"""
        if not self._connected:
            return None
        try:
            contract = self.get_contract(symbol)
            if not contract:
                return None
            snap = self._api.snapshots([contract])
            if not snap:
                return None
            s = snap[0]
            return {
                "symbol": symbol,
                "code": getattr(s, "code", symbol),
                "name": getattr(s, "name", ""),
                "open": float(getattr(s, "open", 0) or 0),
                "high": float(getattr(s, "high", 0) or 0),
                "low": float(getattr(s, "low", 0) or 0),
                "close": float(getattr(s, "close", 0) or 0),
                "volume": int(getattr(s, "volume", 0) or 0),
                "amount": int(getattr(s, "amount", 0) or 0),
            }
        except Exception as e:
            log.error(f"取快照失敗 ({symbol}): {e}")
            return None


# 全域單例
broker = Broker()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        if broker.connect():
            for sym in config.symbols:
                print(f"\n=== {sym} 當前快照 ===")
                print(broker.get_snapshot(sym))
            broker.disconnect()
    else:
        print(f"  模擬環境: {SHIOAJI_SIMULATED}")
        print(f"  API Key 前 8: {SHIOAJI_API_KEY[:8]!r}")
