"""
軍師系統 — 策略單元測試 (tests/test_strategies.py)
純邏輯測試,不連線 Shioaji,不打 LLM/Telegram。驗證 R1-R4 觸發條件與門檻讀取。

執行: python tests/test_strategies.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from config import config  # noqa: E402
from core.strategies import CooldownGate, StrategyDetector  # noqa: E402


def test_r1_triggers_on_enough_big_buys():
    d = StrategyDetector()
    params = config.strategy_params("R1")
    now = datetime.now()
    sig = ""
    for _ in range(params["min_count"]):
        sig, detail = d.feed("TEST1", now, params["min_qty"], "buy", 25.5)
    assert sig == "R1", f"預期 R1 觸發,實際: {sig}"
    assert detail["R1"]["count"] == params["min_count"]
    print("✅ test_r1_triggers_on_enough_big_buys")


def test_r1_does_not_trigger_below_threshold():
    d = StrategyDetector()
    params = config.strategy_params("R1")
    now = datetime.now()
    sig = ""
    for _ in range(params["min_count"] - 1):
        sig, _ = d.feed("TEST2", now, params["min_qty"], "buy", 25.5)
    assert sig == "", f"門檻不足不該觸發,實際: {sig}"
    print("✅ test_r1_does_not_trigger_below_threshold")


def test_r3_net_buy_triggers():
    d = StrategyDetector()
    params = config.strategy_params("R3")
    now = datetime.now()
    price = 25.5
    threshold_lots = params["amount_divisor"] / price
    sig, detail = d.feed("TEST3", now, int(threshold_lots) + 100, "buy", price)
    assert sig == "R3", f"預期 R3 觸發,實際: {sig}"
    assert detail["R3"]["net_lots"] > detail["R3"]["threshold_lots"]
    print("✅ test_r3_net_buy_triggers")


def test_r3_cooldown_blocks_repeat():
    d = StrategyDetector()
    params = config.strategy_params("R3")
    now = datetime.now()
    price = 25.5
    threshold_lots = int(params["amount_divisor"] / price) + 100
    sig1, _ = d.feed("TEST4", now, threshold_lots, "buy", price)
    assert sig1 == "R3"
    # 同一秒內立刻再送一次大單,冷卻期內不該再觸發 R3
    sig2, _ = d.feed("TEST4", now, threshold_lots, "buy", price)
    assert sig2 != "R3", f"冷卻期內不該再觸發 R3,實際: {sig2}"
    print("✅ test_r3_cooldown_blocks_repeat")


def test_r4_counter_scoring():
    """直接測 _check_r4 靜態方法,避免 R1/R2 用同樣的 qty 門檻先觸發並清空 buffer 干擾。"""
    params = config.strategy_params("R4")
    now = datetime.now().timestamp()
    qty = params["min_qty"] + 1
    buf = [(now, qty, "buy", 25.5) for _ in range(params["trigger_count"] + 1)]
    hit, detail = StrategyDetector._check_r4(buf, params, return_detail=True)
    assert hit, f"預期 R4 觸發(counter={detail['counter']} 應 > {params['trigger_count']})"
    assert detail["counter"] == params["trigger_count"] + 1
    print("✅ test_r4_counter_scoring")


def test_auction_window_is_filtered():
    d = StrategyDetector()
    auction_start_s, _ = config.auction_window
    h, m, s = [int(x) for x in auction_start_s.split(":")]
    ts = datetime.now().replace(hour=h, minute=m, second=s, microsecond=0)
    sig, detail = d.feed("TEST6", ts, 999, "buy", 25.5)
    assert sig == "", "開盤試撮第一筆應該被過濾"
    print("✅ test_auction_window_is_filtered")


def test_cooldown_gate_blocks_within_window():
    gate = CooldownGate(seconds=300)
    assert gate.allow("2883", "R1") is True
    assert gate.allow("2883", "R1") is False, "冷卻期內第二次應被擋"
    assert gate.allow("2883", "R2") is True, "不同訊號不共用冷卻"
    print("✅ test_cooldown_gate_blocks_within_window")


def test_thresholds_are_config_driven_not_hardcoded():
    """確認改 config 就能改行為,證明門檻不是寫死的。"""
    original = config.strategy_params("R1")
    try:
        config.update("strategy.R1", {**original, "min_count": 2})
        d = StrategyDetector()
        now = datetime.now()
        sig = ""
        for _ in range(2):
            sig, _ = d.feed("TEST7", now, original["min_qty"], "buy", 25.5)
        assert sig == "R1", "調低 min_count 後應該用新門檻觸發"
        print("✅ test_thresholds_are_config_driven_not_hardcoded")
    finally:
        config.update("strategy.R1", original)  # 還原,不污染其他測試/正式環境


if __name__ == "__main__":
    test_r1_triggers_on_enough_big_buys()
    test_r1_does_not_trigger_below_threshold()
    test_r3_net_buy_triggers()
    test_r3_cooldown_blocks_repeat()
    test_r4_counter_scoring()
    test_auction_window_is_filtered()
    test_cooldown_gate_blocks_within_window()
    test_thresholds_are_config_driven_not_hardcoded()
    print("\n🎉 全部單元測試通過")
