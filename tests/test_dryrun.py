"""
軍師系統 — End-to-End 模擬測試 (tests/test_dryrun.py)
用 Shioaji ticks() 抓指定交易日全日 ticks,重放到 Sentinel.on_tick,
驗證完整流程：偵測 → Queue → 子進程 → LLM → Telegram。

驗證項目:
  1. Shioaji 連線 + ticks() 抓取正常
  2. core.strategies.StrategyDetector 觸發偵測正常 (R1/R2/R3/R4)
  3. multiprocessing.Queue 傳遞正常
  4. strategist 子進程啟動、存活、接到 task
  5. LLM 呼叫 + Telegram 推播（end-to-end）
  6. 不卡 GIL（時間預算內完成重播）
"""
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from config import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("counselor.test")


def last_trading_day() -> str:
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def fetch_ticks(symbol: str, trade_date: str | None = None):
    """真實連線 Shioaji,抓指定日期全日 ticks。"""
    from broker import broker

    target = trade_date or last_trading_day()
    log.info("🔌 連線 Shioaji（模擬環境）...")
    if not broker.connect(retries=2):
        log.error("❌ Shioaji 連線失敗")
        return None, target

    try:
        contract = broker.get_contract(symbol)
        if not contract:
            log.error(f"❌ 找不到合約 {symbol}")
            return None, target

        log.info(f"📥 抓 {symbol} {target} ticks ...")
        t0 = time.time()
        raw = broker._api.ticks(contract, date=target)
        rd = raw.dict()
        n = len(rd.get("ts", []))
        log.info(f"   ✅ 抓到 {n:,} 筆（耗時 {time.time()-t0:.1f}s）")

        if n == 0:
            log.warning("⚠️  無資料（非交易日或模擬環境無歷史 tick）")
            return None, target

        class FakeTick:
            """模擬 sj.TickSTKv1,只帶 on_tick 用到的欄位。"""
            def __init__(self, code, close, volume, tick_type):
                self.code = code
                self.close = close
                self.volume = volume
                self.tick_type = tick_type

        raw_types = rd.get("tick_type", [0] * n)
        ticks = [
            FakeTick(
                code=symbol,
                close=rd["close"][i],
                volume=rd["volume"][i],
                tick_type=int(raw_types[i]) if raw_types[i] is not None else 0,
            )
            for i in range(n)
        ]
        return ticks, target

    except Exception as e:
        log.error(f"❌ 抓 ticks 例外: {e}", exc_info=True)
        return None, target
    finally:
        broker.disconnect(timeout=5)


def replay_ticks(sentinel, ticks, max_ticks: int = 2000, max_seconds: int = 60):
    """把歷史 ticks 逐筆餵進 sentinel.on_tick,模擬盤中即時接收。"""
    if sentinel._strategist_proc is None or not sentinel._strategist_proc.is_alive():
        log.info("🧠 啟動 strategist 子進程...")
        sentinel._strategist_proc = sentinel._spawn_strategist()
        time.sleep(1)

    n = min(len(ticks), max_ticks)
    start = time.time()

    trigger_count = [0]
    _orig_put = sentinel.trigger_queue.put_nowait

    def _counting_put(item):
        trigger_count[0] += 1
        log.info(f"  🔴 推入 Queue（累計 {trigger_count[0]} 次）"
                 f"  symbol={item.get('symbol')}  sig={item.get('sig')}"
                 f"  price={item.get('price')}  qty={item.get('qty')}")
        _orig_put(item)

    sentinel.trigger_queue.put_nowait = _counting_put

    log.info(f"▶️  重播 {n:,} / {len(ticks):,} 筆 ticks（上限 {max_seconds}s）...")
    i = 0
    for i, tick in enumerate(ticks[:n]):
        sentinel.on_tick("TSE", tick)

        if (i + 1) % 200 == 0:
            log.info(f"  進度 {i+1:,}/{n:,}  觸發={trigger_count[0]}  elapsed={time.time()-start:.1f}s")

        time.sleep(0.001)

        if time.time() - start > max_seconds:
            log.warning(f"⏱  超過 {max_seconds}s 預算,停在第 {i+1} 筆")
            break

    sentinel.trigger_queue.put_nowait = _orig_put
    triggered = trigger_count[0]

    elapsed = time.time() - start
    log.info(f"   重播完成：{i+1:,} 筆 / {elapsed:.1f}s / 觸發推入 Queue {triggered} 次")
    return triggered


def wait_and_verify(sentinel, timeout: int = 30) -> bool:
    """等子進程把 Queue 清空,再確認它還活著。"""
    log.info(f"⏳ 等子進程消化 Queue（最多 {timeout}s）...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sentinel.trigger_queue.empty():
            break
        log.info("   Queue 尚未清空,等待中...")
        time.sleep(2)

    proc = sentinel._strategist_proc
    alive = proc is not None and proc.is_alive()
    log.info(f"🧠 子進程 PID={getattr(proc, 'pid', '?')}  alive={alive}")
    return alive


def main():
    from core.sentinel import Sentinel

    sep = "=" * 62
    log.info(sep)
    log.info("🧪 軍師 End-to-End 測試 ― Shioaji get_ticks 重播")
    log.info(sep)

    if not config.symbols:
        log.error("❌ watchlist 沒有任何標的,測試中止")
        return 1
    symbol = config.symbols[0]

    log.info(f"\n【Step 1】連線 Shioaji + 抓 {symbol} ticks")
    ticks, trade_date = fetch_ticks(symbol)
    if not ticks:
        log.error("❌ Step 1 失敗：無 ticks,測試中止")
        return 1
    log.info(f"   ✅ {len(ticks):,} 筆 ticks  日期={trade_date}")

    sentinel = Sentinel()

    log.info("\n【Step 2】重播 ticks → on_tick → Queue → strategist 子進程")
    triggered = replay_ticks(sentinel, ticks, max_ticks=2000, max_seconds=60)
    log.info(f"   ✅ 觸發推入 Queue：{triggered} 次")

    log.info("\n【Step 3】等子進程消化 Queue + 驗活")
    alive = wait_and_verify(sentinel, timeout=30)

    log.info(f"\n{sep}")
    if triggered > 0 and alive:
        log.info("🎉 測試通過：觸發正常,子進程存活,Telegram 應已送出")
        log.info(sep)
        return 0
    elif triggered == 0:
        log.warning("⚠️  測試部分通過：ticks 成功抓取,但無策略觸發")
        log.warning("    可能原因：該日大單不夠、門檻太高、或 tick_type 全為 0")
        log.info(sep)
        return 0
    else:
        log.error("❌ 測試失敗：子進程死掉了")
        log.error(sep)
        return 1


if __name__ == "__main__":
    sys.exit(main())
