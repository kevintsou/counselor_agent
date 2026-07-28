"""
軍師系統 — 輸出格式測試 (tests/test_format.py)
驗證密令四欄解析、金額換算、五檔/tape 格式化。純函式,不連 Telegram/LLM。

執行: python tests/test_format.py
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import notify.herald as h  # noqa: E402
import llm.router_client as rc  # noqa: E402


def test_parse_new_four_field():
    order = "動作: 買\n研判: 主力單向吃貨,三層一致偏多。\n失效: 跌破 20.00\n風險: 單筆 ≤ 2 張"
    p = h._parse_order(order)
    assert p is not None
    assert "買" in p["動作"]
    assert "主力單向吃貨" in p["研判"]
    assert p["失效"] == "跌破 20.00"
    assert p["風險"] == "單筆 ≤ 2 張"
    print("✅ test_parse_new_four_field")


def test_parse_old_bracket_format():
    """舊【】格式要能相容:依據→研判、失效/風險合併欄。"""
    p = h._parse_order("【動作】觀望\n【依據】量縮價穩\n【失效/風險】跌破 1000")
    assert p is not None
    assert "觀望" in p["動作"]
    assert "量縮價穩" in p["研判"]
    assert "1000" in p["失效"]
    print("✅ test_parse_old_bracket_format")


def test_parse_multiline_research():
    """研判跨行要能續接。"""
    p = h._parse_order("動作: 買\n研判: 第一句。\n這是換行的第二句。\n失效: 跌破 20")
    assert p is not None
    assert "第一句" in p["研判"] and "第二句" in p["研判"]
    print("✅ test_parse_multiline_research")


def test_parse_garbage_returns_none():
    """完全無欄位 → 回 None,讓 send_order 走原文回退不丟失。"""
    assert h._parse_order("主力在買啦快進場") is None
    print("✅ test_parse_garbage_returns_none")


def test_fmt_money_scales():
    assert "億" in h._fmt_money(108_240_000)
    assert "萬" in h._fmt_money(410_000)
    assert h._fmt_money(5000).startswith("$")
    print("✅ test_fmt_money_scales")


def test_action_style_colors():
    assert h._action_style("買")[0] == "🟢"
    assert h._action_style("賣")[0] == "🔴"
    assert h._action_style("觀望")[0] == "⚪"
    print("✅ test_action_style_colors")


def test_bidask_compact_imbalance():
    ba = {"bid_price": [20.5, 20.45], "bid_volume": [45, 30],
          "ask_price": [20.55, 20.6], "ask_volume": [12, 8], "ts": "09:03"}
    out = h._format_bidask_compact(ba)
    assert "委買 20.50×45" in out and "偏買" in out
    assert h._format_bidask_compact(None) == ""  # 無盤口安全回空
    print("✅ test_bidask_compact_imbalance")


def test_router_formatters_empty_safe():
    """五檔/tape 缺資料時不能炸,要回可讀提示。"""
    assert "無五檔" in rc._format_bidask(None)
    assert "無逐筆" in rc._format_tape([])
    tape = [{"ts": "09:03:01", "qty": 50, "side": "buy", "price": 20.5}]
    assert "買 50 張" in rc._format_tape(tape)
    print("✅ test_router_formatters_empty_safe")


if __name__ == "__main__":
    test_parse_new_four_field()
    test_parse_old_bracket_format()
    test_parse_multiline_research()
    test_parse_garbage_returns_none()
    test_fmt_money_scales()
    test_action_style_colors()
    test_bidask_compact_imbalance()
    test_router_formatters_empty_safe()
    print("\n🎉 全部格式測試通過")
