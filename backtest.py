"""
軍師系統 — 回測腳本 (backtest.py)
用 Shioaji ticks() 抓多個交易日全 ticks,重播進 StrategyDetector.feed(),
統計 R1/R2/R3/R4 觸發次數,驗證策略參數。

v2(模組化重構):不再 import sentinel 的模組級狀態,直接用 core.strategies.StrategyDetector,
backtest 與 sentinel 徹底解耦,兩者都只依賴 core/ 這個共用邏輯層。
"""
import logging
import multiprocessing
import time
from datetime import datetime, timedelta

from broker import broker
from config import config
from core.strategies import StrategyDetector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("counselor.backtest")

DAYS_BACK = 5
# 加速:每筆 ticks = 0.05 秒(20x 加速,真實 1.6s/筆 → 0.08s/筆)
TICK_INTERVAL = 0.05


def get_recent_trade_dates(n: int):
    """取最近 n 個交易日(跳過週末)。"""
    dates = []
    d = datetime.now() - timedelta(days=1)
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return dates


def fetch_ticks(symbol: str, trade_date: str):
    """用 Shioaji 抓某檔某日 ticks。"""
    try:
        contract = broker.get_contract(symbol)
        if not contract:
            return None
        raw = broker._api.ticks(contract, date=trade_date)
        rd = raw.dict()
        n = len(rd.get("ts", []))
        if n == 0:
            return None
        log.info(f"     ticks={n}, 首筆 ts={rd['ts'][0]}")
        return rd
    except Exception as e:
        log.warning(f"  抓 {symbol} {trade_date} 失敗: {e}")
        return None


def replay_symbol_on_day(detector: StrategyDetector, queue: multiprocessing.Queue, symbol: str, rd: dict) -> dict:
    """用模擬 wall clock 重播一天的 ticks 進 detector.feed。"""
    stats = {
        "tick_count": 0, "r1_count": 0, "r3_count": 0, "other_count": 0,
        "sent_to_subproc": 0, "dropped_queue_full": 0,
    }
    triggers: list = []

    n = len(rd["ts"])
    base_ts = time.time()

    for i in range(n):
        real_ts = datetime.fromtimestamp(base_ts + i * TICK_INTERVAL)
        qty = int(rd["volume"][i])
        tick_type = rd.get("tick_type", [0] * n)[i] or 0
        if tick_type in (1, "1", "Buy"):
            side = "buy"
        elif tick_type in (2, -1, "2", "-1", "Sell"):
            side = "sell"
        else:
            side = "unknown"
        price = float(rd["close"][i])

        sig, detail = detector.feed(symbol, real_ts, qty, side, price)
        stats["tick_count"] += 1

        if sig:
            if sig == "R1":
                stats["r1_count"] += 1
            elif sig == "R3":
                stats["r3_count"] += 1
            else:
                stats["other_count"] += 1
            triggers.append({
                "symbol": symbol, "sig": sig, "detail": detail,
                "qty": qty, "side": side, "price": price, "ts": real_ts.isoformat(),
            })

        if i % 50 == 0:
            time.sleep(0.0001)  # 避免 CPU 100%

    # 重播跑完才一次 put(避免中途卡 feeder lock)
    for t in triggers:
        try:
            queue.put_nowait(t)
            stats["sent_to_subproc"] += 1
        except Exception:
            stats["dropped_queue_full"] += 1
    return stats


def run_backtest():
    symbols = config.symbols
    log.info("=" * 60)
    log.info(f"🧪 台股軍師 回測 — 過去 {DAYS_BACK} 個交易日 × {len(symbols)} 檔(加速 {1.6/TICK_INTERVAL:.0f}x)")
    log.info("=" * 60)

    log.info("📡 連線 Shioaji...")
    if not broker.connect(retries=2):
        log.error("❌ 連線失敗")
        return 1

    dates = get_recent_trade_dates(DAYS_BACK)
    log.info(f"📅 交易日:{dates}")

    trigger_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=100)

    grand_total = {
        "tick_count": 0, "r1_count": 0, "r3_count": 0, "other_count": 0,
        "sent_to_subproc": 0, "dropped_queue_full": 0,
    }
    per_symbol_per_day = {}

    for sym in symbols:
        per_symbol_per_day[sym] = {}
        for d in dates:
            log.info(f"  📥 {sym} {d} ...")
            t0 = time.time()
            rd = fetch_ticks(sym, d)
            if not rd:
                log.warning("     沒資料,跳過")
                per_symbol_per_day[sym][d] = None
                continue
            log.info(f"     抓到 {len(rd['ts']):,} 筆 ticks(耗時 {time.time()-t0:.1f}s)")

            detector = StrategyDetector()  # 每天重置,避免跨日狀態污染
            stats = replay_symbol_on_day(detector, trigger_queue, sym, rd)
            per_symbol_per_day[sym][d] = stats

            for k, v in stats.items():
                grand_total[k] += v
            log.info(
                f"     統計: ticks={stats['tick_count']:>6,} "
                f"R1={stats['r1_count']:>3} R3={stats['r3_count']:>3} "
                f"推={stats['sent_to_subproc']:>3}"
            )

    broker.disconnect(timeout=5)

    log.info("")
    log.info("=" * 60)
    log.info(f"📊 回測報告({DAYS_BACK} 個交易日 × {len(symbols)} 檔)")
    log.info("=" * 60)
    log.info(f"總 ticks:          {grand_total['tick_count']:>10,}")
    log.info(f"R1 觸發:           {grand_total['r1_count']:>10,}")
    log.info(f"R3 觸發:           {grand_total['r3_count']:>10,}")
    log.info(f"其他觸發:          {grand_total['other_count']:>10,}")
    log.info(f"推到 queue 成功:   {grand_total['sent_to_subproc']:>10,}")
    log.info(f"Queue 滿丟棄:      {grand_total['dropped_queue_full']:>10,}")
    log.info("")
    log.info("每日明細:")
    for sym in symbols:
        log.info(f"  {sym}:")
        for d, st in per_symbol_per_day[sym].items():
            if st is None:
                log.info(f"    {d}: (沒資料)")
            else:
                log.info(
                    f"    {d}: ticks={st['tick_count']:>6,} "
                    f"R1={st['r1_count']:>3} R3={st['r3_count']:>3} "
                    f"推={st['sent_to_subproc']:>3}"
                )

    if grand_total["tick_count"] > 0:
        total_signals = grand_total["r1_count"] + grand_total["r3_count"] + grand_total["other_count"]
        log.info("")
        log.info("=" * 60)
        log.info("🧮 預估實際運作(扣掉 queue 滿):")
        log.info(f"  {DAYS_BACK} 天訊號數: {total_signals}")
        log.info(f"  預估 LLM 呼叫(假設全消化): {total_signals:,} 次/{DAYS_BACK}天")
        log.info(f"  = 約 {total_signals / DAYS_BACK:.1f} 次/天")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(run_backtest())
