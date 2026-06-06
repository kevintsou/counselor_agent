"""
軍師系統 — 回測腳本 (backtest.py)
用 Shioaji get_ticks() 抓多個交易日全 ticks,重播進 on_tick 流程
統計 R1/R2/R3/R4 觸發 + cooldown 過濾,驗證策略參數
"""
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))
from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("counselor.backtest")


def _load_symbols() -> list[str]:
    """從 watchlist.yaml 讀取回測標的，跟 sentinel 保持同一份清單。"""
    wl = _ROOT / "watchlist.yaml"
    if not wl.exists():
        log.error(f"找不到 watchlist.yaml: {wl}")
        return []
    with open(wl, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [s["symbol"] for s in data.get("stocks", [])]


# 回測設定（標的從 watchlist.yaml 動態讀取）
SYMBOLS   = _load_symbols()
DAYS_BACK = 5  # 5 個交易日
# 加速:每筆 ticks = 0.05 秒(20x 加速,真實 1.6s/筆 → 0.08s/筆)
# 5 秒視窗(R1) = 100 筆 ticks,密度約為真實的 20 倍
# 2883 一日 10000 筆 → 800s/日,5 天 2 檔 ≈ 2.2 小時(可接受)
TICK_INTERVAL = 0.05  # 20x 加速


def get_recent_trade_dates(n: int):
    """取最近 n 個交易日(跳過週末)。"""
    dates = []
    d = datetime.now() - timedelta(days=1)
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return dates


def fetch_ticks(broker, symbol: str, date: str):
    """用 Shioaji 抓某檔某日 ticks。"""
    try:
        contract = broker.get_contract(symbol)
        if not contract:
            return None
        raw = broker._api.ticks(contract, date=date)
        rd = raw.dict()
        n = len(rd.get("ts", []))
        if n == 0:
            return None
        first_ts = rd["ts"][0]
        # Shioaji 模擬環境的 ticks ts 不是真實時間戳(可能是序號)
        # 不用修補,直接用 idx 當 ts(回測只在意時間相對差,不在意實際日期)
        log.info(f"     ticks={len(rd['ts'])}, 首筆 ts={first_ts} (僅用 idx 計時)")
        return rd
    except Exception as e:
        log.warning(f"  抓 {symbol} {date} 失敗: {e}")
        return None


def replay_symbol_on_day(detector, queue, symbol: str, rd: dict) -> dict:
    """用真實 ts 重播一天的 ticks 進 detector.feed。"""
    stats = {
        "tick_count": 0,
        "r1_count": 0,
        "r3_count": 0,
        "other_count": 0,
        "sent_to_subproc": 0,
        "filtered_by_cooldown": 0,
    }
    triggers: list = []  # 先收 list,replay 完再一次 put queue(避免 feeder lock 衝突)
    triggers: list = []  # v3.1:改用 list,跑完再 put queue

    n = len(rd["ts"])
    # 用真實 ticks ts(如果可用)或 idx(若 Shioaji 模擬環境 ts 不規律)
    # v3 改:就用「真實 wall clock」+「每筆 sleep」,讓 R3 cooldown 60 秒真的走過
    # 2883 一日約 10000 筆,真實 270 分鐘 → 每筆間距 1.6 秒
    # 加速 100x → sleep 0.016s/筆
    TICK_INTERVAL = 0.05  # 20x 加速(與 module 一致)
    base_ts = time.time()  # 從現在開始的 wall clock

    for i in range(n):
        # 用 wall clock 模擬真實時序
        real_ts = datetime.fromtimestamp(base_ts + i * TICK_INTERVAL)
        qty = int(rd["volume"][i])
        tick_type = rd.get("tick_type", [0] * n)[i] or 0
        side = "buy" if tick_type in (1, "1", "Buy") else "sell"
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
            # v3.1 修:不在這裡 put queue(會搶 feeder lock 卡住)
            # 改在 replay 跑完後統一 put
            triggers.append({
                "symbol": symbol, "sig": sig, "detail": detail,
                "qty": qty, "side": side, "price": price, "ts": real_ts.isoformat(),
            })

        # 真實 sleep(加速模式)
        if i % 50 == 0:
            time.sleep(0.0001)  # 小 sleep 避免 CPU 100%

    # v3.1 修:replay 跑完才一次 put(避免 main process 內部 feeder lock 卡住)
    for t in triggers:
        try:
            queue.put_nowait(t)
            stats["sent_to_subproc"] += 1
        except Exception:
            stats["filtered_by_cooldown"] += 1
    return stats


def run_backtest():
    log.info("=" * 60)
    log.info(f"🧪 台股軍師 回測 — 過去 {DAYS_BACK} 個交易日 × {len(SYMBOLS)} 檔(加速 {1.6/TICK_INTERVAL:.0f}x)")
    log.info("=" * 60)

    from sentinel import detector, _TRIGGER_QUEUE
    from broker import broker

    log.info("📡 連線 Shioaji...")
    if not broker.connect(retries=2):
        log.error("❌ 連線失敗")
        return 1

    dates = get_recent_trade_dates(DAYS_BACK)
    log.info(f"📅 交易日:{dates}")

    grand_total = {
        "tick_count": 0,
        "r1_count": 0,
        "r3_count": 0,
        "other_count": 0,
        "sent_to_subproc": 0,
        "filtered_by_cooldown": 0,
    }

    per_symbol_per_day = {}

    for sym in SYMBOLS:
        per_symbol_per_day[sym] = {}
        for d in dates:
            log.info(f"  📥 {sym} {d} ...")
            t0 = time.time()
            rd = fetch_ticks(broker, sym, d)
            if not rd:
                log.warning(f"     沒資料,跳過")
                per_symbol_per_day[sym][d] = None
                continue
            log.info(f"     抓到 {len(rd['ts']):,} 筆 ticks(耗時 {time.time()-t0:.1f}s)")

            # 重播前重置 detector(避免跨日狀態污染)
            from sentinel import StrategyDetector
            detector.__init__()

            stats = replay_symbol_on_day(detector, _TRIGGER_QUEUE, sym, rd)
            per_symbol_per_day[sym][d] = stats

            for k, v in stats.items():
                grand_total[k] += v
            log.info(
                f"     統計: ticks={stats['tick_count']:>6,} "
                f"R1={stats['r1_count']:>3} R3={stats['r3_count']:>3} "
                f"推={stats['sent_to_subproc']:>3} 濾={stats['filtered_by_cooldown']:>3}"
            )

    broker.disconnect(timeout=5)

    log.info("")
    log.info("=" * 60)
    log.info(f"📊 回測報告({DAYS_BACK} 個交易日 × {len(SYMBOLS)} 檔)")
    log.info("=" * 60)
    log.info(f"總 ticks:          {grand_total['tick_count']:>10,}")
    log.info(f"R1 觸發:           {grand_total['r1_count']:>10,}")
    log.info(f"R3 觸發:           {grand_total['r3_count']:>10,}")
    log.info(f"其他觸發:          {grand_total['other_count']:>10,}")
    log.info(f"推到 queue 成功:   {grand_total['sent_to_subproc']:>10,}")
    log.info(f"Queue 滿丟棄:      {grand_total['filtered_by_cooldown']:>10,}")
    log.info("")
    log.info("每日明細:")
    for sym in SYMBOLS:
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

    # 預估實際運作
    if grand_total['tick_count'] > 0:
        total_signals = grand_total['r1_count'] + grand_total['r3_count'] + grand_total['other_count']
        log.info("")
        log.info("=" * 60)
        log.info("🧮 預估實際運作(扣掉 queue 滿):")
        log.info(f"  5 天訊號數: {total_signals}")
        log.info(f"  預估 LLM 呼叫(假設全消化): {total_signals:,} 次/5天")
        log.info(f"  = 約 {total_signals / 5:.1f} 次/天")
        log.info(f"  MiniMax 額度(假設單次 200 字,平均 500 token):約 {total_signals * 500 / 5:.0f} token/天")

    return 0


if __name__ == "__main__":
    sys.exit(run_backtest())
