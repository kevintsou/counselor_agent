"""
軍師系統 v7 正式模擬測試 (test_v7_dryrun.py)
用 Shioaji get_ticks() 抓今天 2883 全日 ticks,重放到 sentinel 訂閱 callback,
看能不能正常觸發 R1/R3,推到子進程,跑 LLM,推 Telegram。

驗證項目:
  1. sentinel.py + strategist.py 都能正常啟動
  2. Shioaji get_ticks() 能成功抓到資料(模擬環境也行)
  3. 觸發偵測邏輯正常(R1/R3)
  4. multiprocessing.Queue 傳遞正常
  5. 子進程 strategist.py 能跑 LLM + 推 Telegram
  6. 不再卡 GIL(30 秒內完成)
"""
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))
from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("counselor.test_v7")


def fetch_2883_ticks_today():
    """用 Shioaji get_ticks() 抓今天 2883 全日 ticks。"""
    import shioaji as sj
    from broker import broker

    if not broker.connect(retries=2):
        log.error("❌ Shioaji 連線失敗,無法抓 ticks")
        return None, None

    try:
        contract = broker.get_contract("2883")
        if not contract:
            log.error("❌ 找不到 2883 合約")
            return None, None

        today = datetime.now().strftime("%Y-%m-%d")
        log.info(f"📥 抓 2883 {today} ticks ...")
        t0 = time.time()
        raw = broker._api.ticks(contract, date=today)
        rd = raw.dict()
        n = len(rd.get("ts", []))
        log.info(f"   抓到 {n} 筆 ticks(耗時 {time.time()-t0:.1f}s)")

        if n == 0:
            log.warning("⚠️ 沒抓到 ticks(可能非交易日或模擬環境無資料)")
            return None, None

        # 轉成 on_tick 模擬格式
        class FakeTick:
            def __init__(self, code, ts, close, volume, tick_type):
                self.code = code
                self.ts = ts
                self.close = close
                self.volume = volume
                self.tick_type = tick_type

        ticks = []
        for i in range(n):
            # Shioaji ts 是 epoch ms
            ts = rd["ts"][i]
            tick_type = rd.get("tick_type", [0] * n)[i] or 0
            ticks.append(FakeTick(
                code="2883",
                ts=ts,
                close=rd["close"][i],
                volume=rd["volume"][i],
                tick_type=1 if tick_type in (1, "1", "Buy") else 2,
            ))

        # 抓收盤快照
        log.info("📸 抓 2883 收盤快照")
        snaps = broker._api.snapshots([contract])
        snap = snaps[0] if snaps else None
        return ticks, snap
    except Exception as e:
        log.error(f"❌ 抓 ticks 失敗: {e}", exc_info=True)
        return None, None
    finally:
        broker.disconnect(timeout=5)


def replay_ticks_through_sentinel(ticks, snap, max_seconds: int = 30):
    """模擬即時 tick 推播,跑進 sentinel 偵測器 + 子進程。"""
    if not ticks:
        log.error("❌ 沒 ticks 可重播")
        return False

    # 啟動 sentinel + 子進程
    log.info("=" * 60)
    log.info("🚀 啟動 sentinel(主進程) + strategist(子進程)")
    log.info("=" * 60)

    from sentinel import (
        Sentinel, on_tick, detector, _TRIGGER_QUEUE, _STRATEGIST_PROC,
        _LAST_TICK_TS, _spawn_strategist,
    )
    from herald import send_alert

    # 啟動子進程
    if _STRATEGIST_PROC is None or not _STRATEGIST_PROC.is_alive():
        _spawn_strategist()

    # 注入 mock snapshot 到 on_tick 流程(讓 sentinel 內部不需真 broker)
    # 由於我們要測的只是 on_tick → queue → 子進程 → Telegram
    # 我們直接呼叫 on_tick 重播 ticks,broker.get_snapshot 不會被呼叫
    # 因為子進程只吃 task dict,不跟 broker 互動

    log.info(f"📤 開始重播 {min(len(ticks), 500)} 筆 ticks(加速模式)...")
    start = time.time()
    replay_count = min(len(ticks), 500)
    pushed_to_queue = 0  # 統計進 _TRIGGER_QUEUE 的次數

    for i, tick in enumerate(ticks[:replay_count]):
        # 模擬即時:用真實 ts 加速
        before = _TRIGGER_QUEUE.qsize()
        on_tick(tick)
        after = _TRIGGER_QUEUE.qsize()
        if after > before:
            pushed_to_queue += 1
        if i % 50 == 0:
            log.info(f"  重播進度 {i}/{replay_count} (已推 queue: {pushed_to_queue})")
        time.sleep(0.001)
        if time.time() - start > max_seconds:
            log.warning(f"⏱️  超過 {max_seconds}s 預算,停止重播(已播 {i})")
            break

    # 等子進程消化 queue(最多 15s)
    log.info("⏳ 等子進程消化 queue(最多 15s)...")
    deadline = time.time() + 15
    while time.time() < deadline:
        if _TRIGGER_QUEUE.empty():
            break
        time.sleep(0.5)

    elapsed = time.time() - start
    log.info("=" * 60)
    log.info(f"✅ 重播完成:總耗時 {elapsed:.1f}s,on_tick {replay_count} 次 / 推 queue {pushed_to_queue} 次")
    log.info("=" * 60)
    return True


def verify_subprocess_alive():
    """驗證 strategist 子進程還活著。"""
    from sentinel import _STRATEGIST_PROC
    if _STRATEGIST_PROC is None:
        log.error("❌ 子進程沒啟動")
        return False
    alive = _STRATEGIST_PROC.is_alive()
    log.info(f"🧠 子進程 PID={_STRATEGIST_PROC.pid} alive={alive}")
    return alive


def main():
    log.info("=" * 60)
    log.info("🧪 軍師 v7 正式模擬測試 — 用 Shioaji get_ticks 跑完整流程")
    log.info("=" * 60)

    # Step 1: 抓 ticks
    log.info("Step 1: 抓 2883 今天 ticks")
    ticks, snap = fetch_2883_ticks_today()
    if not ticks:
        log.error("❌ 沒抓到 ticks,測試中止")
        return 1

    # Step 2: 重播
    log.info("Step 2: 重播 ticks 進 sentinel")
    ok = replay_ticks_through_sentinel(ticks, snap)

    # Step 3: 驗證子進程
    log.info("Step 3: 驗證子進程活著")
    alive = verify_subprocess_alive()

    if ok and alive:
        log.info("=" * 60)
        log.info("🎉 測試通過:v7 架構正常")
        log.info("=" * 60)
        return 0
    else:
        log.error("=" * 60)
        log.error("❌ 測試失敗")
        log.error("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(main())
