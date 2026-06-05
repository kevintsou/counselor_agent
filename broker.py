"""
軍師系統 — 券商連線 (broker.py)
封裝 Shioaji: 登入 / 訂閱 Tick / 抓 K 線 / 取快照。

設計原則:
- 模擬環境優先 (simulation=True),真實帳號需明確 SHIOAJI_SIMULATED=0
- Tick 走 callback,不阻塞主迴圈
- 內建 timeout / 重連 (3 次)
"""
import logging
import os
import signal
import time
from pathlib import Path
from typing import Optional, Callable
from dotenv import load_dotenv

_ROOT = Path(__file__).parent
load_dotenv(_ROOT / ".env")

log = logging.getLogger("counselor.broker")

SHIOAJI_API_KEY = os.getenv("SHIOAJI_API_KEY", "")
SHIOAJI_SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY", "")
SHIOAJI_PERSON_ID = os.getenv("SHIOAJI_PERSON_ID", "").split()[0]
SHIOAJI_SIMULATED = os.getenv("SHIOAJI_SIMULATED", "1") == "1"


class Broker:
    # Shioaji Solace session event codes (from official docs)
    _EV_SESSION_UP          = 0   # Session established / reconnected
    _EV_SESSION_DOWN        = 1   # Session went down (error)
    _EV_RECONNECTING        = 12  # Auto-reconnect in progress
    _EV_RECONNECTED         = 13  # Auto-reconnect succeeded
    _EV_SUBSCRIBE_OK        = 16  # Subscribe/Unsubscribe ok (noisy, ignored)

    # 手動重連前最多信任 Shioaji 自行恢復的時間
    SESSION_RECOVERY_GRACE  = 180  # seconds — 3 分鐘內讓 Shioaji 自己搞定

    def __init__(self):
        self._api = None
        self._connected = False
        # Session 自動重連狀態追蹤(由 _on_session_event 維護)
        self._session_recovering: bool = False
        self._session_down_ts: float = 0.0

    # ── Session event callback ──────────────────────────────────────────────
    def _on_session_event(self, resp_code: int, event_code: int, info: str, event: str):
        """Shioaji Solace session event callback。

        Shioaji 內建重連機制(最多 50 次),不需要我們介入。
        只做狀態追蹤,讓 sentinel watchdog 知道「正在自動恢復,不要衝進來打架」。

        event_code:
          0  = Session up (連線成功 / 自動恢復成功)
          1  = Session down error (斷線)
          12 = Reconnecting (自動重連進行中)
          13 = Reconnected (自動重連成功)
          16 = Subscribe/Unsubscribe ok (每次訂閱都會跑,噪音,忽略)
        """
        if event_code == self._EV_SUBSCRIBE_OK:
            return  # 太吵,不 log

        log.info(f"📡 Shioaji session [{event_code}] {event}")

        if event_code in (self._EV_SESSION_DOWN, self._EV_RECONNECTING):
            if not self._session_recovering:
                self._session_down_ts = time.time()
                self._session_recovering = True
            # Shioaji 自行恢復中,_connected 暫時維持 True
            # (subscription 層還活著,只是 transport 斷了)

        elif event_code in (self._EV_SESSION_UP, self._EV_RECONNECTED):
            was_recovering = self._session_recovering
            self._session_recovering = False
            self._session_down_ts = 0.0
            self._connected = True
            if was_recovering:
                age = time.time() - self._session_down_ts if self._session_down_ts else 0
                log.info(f"✅ Shioaji session 自動恢復完成")

    @property
    def session_recovering(self) -> bool:
        """Shioaji 正在自動重連中(sentinel watchdog 應讓開)。"""
        return self._session_recovering

    def session_down_age(self) -> float:
        """Session 斷線已持續幾秒。未斷線時回傳 0。"""
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
                # 掛 session event callback:監聽 Shioaji 自動重連狀態
                # 放在 login 成功後,確保 quote 物件已初始化
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
        """登出 Shioaji,加了 timeout 保護避免斷線狀態下 logout() 永久卡住。

        Args:
            timeout: 最多等幾秒(預設 10s)
        """
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
            signal.alarm(0)  # 取消鬧鐘
            signal.signal(signal.SIGALRM, old_handler)  # 還原舊 handler
            # 不論成功 / 逾時 / 例外,都強制標記斷線
            self._connected = False
            self._api = None

    def is_alive(self) -> bool:
        """健康檢查:v6.1.2 最簡化 — 只看 flag。

        修法謁糸(2026-06-04):
          - v6.0 snapshots():太敏感
          - v6.1 getattr():太寬鬆(靜默)
          - v6.1.1 account_balance():模擬環境中偶會卡
          - v6.1.2 只看 flag:斷線偵測改由「訂閱 callback 是否有在
            收到 tick」判斷(tick 連續 60s 沒進來就代表連線死了)
            趨向 「push-based detection」而非「poll-based ping」
        """
        return self._connected

    def reconnect(self, retries: int = 3, resubscribe_callbacks: Optional[list] = None) -> bool:
        """重連流程:logout → login → 重新訂閱。

        Args:
            retries: 最大重試次數(預設 3)
            resubscribe_callbacks: [(symbol, callback, quote_type), ...] 重新訂閱清單
        Returns: True 連線成功 / False 連線失敗
        """
        log.warning("🔄 broker 開始重連程序...")
        # 先清掉舊連線
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
                if self.connect(retries=1):  # 內部不再 retry
                    # 重新訂閱(如果提供清單)
                    if resubscribe_callbacks:
                        for symbol, callback, quote_type in resubscribe_callbacks:
                            ok = self._resubscribe_one(symbol, callback, quote_type)
                            log.info(f"  重新訂閱 {symbol} ({quote_type}): {'✅' if ok else '❌'}")
                    log.info(f"✅ 重連成功(第 {attempt} 次)")
                    return True
            except Exception as e:
                log.warning(f"  重連第 {attempt} 次失敗: {e}")
            time.sleep(2 ** attempt)  # 指數退避: 2s, 4s, 8s
        log.error(f"❌ broker 重連失敗,已重試 {retries} 次")
        return False

    def _resubscribe_one(self, symbol: str, callback: Callable, quote_type: str = "tick") -> bool:
        """內部用:重訂閱單一 symbol(不走 log 重複輸出)。"""
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
        """取得股票合約物件。"""
        if not self._connected:
            return None
        try:
            return self._api.Contracts.Stocks[symbol]
        except KeyError:
            log.error(f"❌ 找不到股票代號: {symbol}")
            return None

    def subscribe_tick(self, symbol: str, callback: Callable) -> bool:
        """訂閱 Tick 回撥(callback 接收 sj.TickEvent)。"""
        if not self._connected:
            log.error("尚未連線,請先 connect()")
            return False
        try:
            contract = self.get_contract(symbol)
            if not contract:
                return False
            # Shioaji 1.5+ 新介面
            self._api.set_on_tick_stk_v1_callback(callback)
            self._api.subscribe(contract, quote_type="tick")
            log.info(f"📡 已訂閱 {symbol} tick")
            return True
        except Exception as e:
            log.error(f"訂閱 tick 失敗 ({symbol}): {e}")
            return False

    def unsubscribe_tick(self, symbol: str) -> bool:
        """取消訂閱 Tick(watchlist 熱重載移除個股時用)。"""
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
        """訂閱五檔委買委賣回撥。"""
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
            # 除錯用: 印出實際屬性
            # print(f"DEBUG {symbol}:", dir(s))
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
            # 抓 2883 快照
            snap = broker.get_snapshot("2883")
            print("\n=== 2883 當前快照 ===")
            print(snap)
            # 抓 2330 快照
            snap2 = broker.get_snapshot("2330")
            print("\n=== 2330 當前快照 ===")
            print(snap2)
            broker.disconnect()
    else:
        print(f"  模擬環境: {SHIOAJI_SIMULATED}")
        print(f"  API Key 前 8: {SHIOAJI_API_KEY[:8]!r}")
        print(f"  Person ID: {SHIOAJI_PERSON_ID}")
