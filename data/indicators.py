"""
軍師系統 — 盤後指標計算 (data/indicators.py)
從 ticks + 五檔 + 三大法人 + 融資券 + 大盤指數 算出指標,分 4 組(A/B/C/D)。
純計算,不做 IO。大單門檻與尾盤分界時刻皆由 config/thresholds.json 的 indicators 區塊控制。
"""
import logging
import statistics
from datetime import datetime
from typing import Optional

from config import config

log = logging.getLogger("counselor.indicators")


def calc_tick_indicators(ticks: list[dict], snap: dict) -> dict:
    """【組 A】Ticks + 五檔 15 項指標。ticks: list of {ts, close, volume, tick_type}"""
    if not ticks:
        return {"_error": "no ticks"}

    ind_cfg = config.indicators_config()
    large_order_threshold = ind_cfg.get("large_order_threshold_lots", 50)
    late_session_ms = ind_cfg.get("late_session_hour", 13) * 60 * 60 * 1000

    opens = ticks[0]["close"]
    closes = [t["close"] for t in ticks]
    highs = max(closes)
    lows = min(closes)
    last = closes[-1]

    amp = (highs - lows) / opens * 100 if opens else 0
    total_vol = sum(t["volume"] for t in ticks)
    total_amt = sum(t["close"] * t["volume"] * 1000 for t in ticks)

    buy_ticks = [t for t in ticks if t["tick_type"] == 1]
    sell_ticks = [t for t in ticks if t["tick_type"] == 2]
    buy_count = len(buy_ticks)
    sell_count = len(sell_ticks)
    ob_ratio = buy_count / sell_count if sell_count else 0

    big_buy = [t for t in buy_ticks if t["volume"] >= large_order_threshold]
    big_sell = [t for t in sell_ticks if t["volume"] >= large_order_threshold]
    big_buy_count = len(big_buy)
    big_sell_count = len(big_sell)
    big_buy_net_vol = sum(t["volume"] for t in big_buy) - sum(t["volume"] for t in big_sell)

    buy_vols = sorted([t["volume"] for t in buy_ticks])
    sell_vols = sorted([t["volume"] for t in sell_ticks])
    buy_median = statistics.median(buy_vols) if buy_vols else 0
    sell_median = statistics.median(sell_vols) if sell_vols else 0

    max_buy = max((t["volume"] for t in buy_ticks), default=0)
    max_sell = max((t["volume"] for t in sell_ticks), default=0)
    max_buy_price = next((t["close"] for t in buy_ticks if t["volume"] == max_buy), 0)
    max_sell_price = next((t["close"] for t in sell_ticks if t["volume"] == max_sell), 0)

    late_ticks = [t for t in ticks if t["ts"] % 86400000 >= late_session_ms]
    late_big_buy_vol = sum(t["volume"] for t in late_ticks if t["volume"] >= large_order_threshold and t["tick_type"] == 1)
    late_big_sell_vol = sum(t["volume"] for t in late_ticks if t["volume"] >= large_order_threshold and t["tick_type"] == 2)
    late_signal = (
        "買" if late_big_buy_vol > late_big_sell_vol * 1.5
        else "賣" if late_big_sell_vol > late_big_buy_vol * 1.5
        else "中性"
    )

    minute_buckets: dict[str, list[float]] = {}
    for t in ticks:
        dt = datetime.fromtimestamp(t["ts"] / 1000)
        key = dt.strftime("%H:%M")
        minute_buckets.setdefault(key, []).append(t["close"])
    minute_changes = []
    prev_close = None
    for minute in sorted(minute_buckets.keys()):
        first_close = minute_buckets[minute][0]
        if prev_close:
            change = (first_close - prev_close) / prev_close * 100
            minute_changes.append((minute, change))
        prev_close = minute_buckets[minute][-1]
    if minute_changes:
        max_up = max(minute_changes, key=lambda x: x[1])
        max_1min_change = max(abs(c) for _, c in minute_changes)
    else:
        max_up = (None, 0)
        max_1min_change = 0

    bucket5: dict[str, int] = {}
    for t in ticks:
        dt = datetime.fromtimestamp(t["ts"] / 1000)
        minute = int(dt.strftime("%M")) // 5 * 5
        key = f"{dt.strftime('%H')}:{minute:02d}"
        bucket5[key] = bucket5.get(key, 0) + t["volume"]
    peak_5min = max(bucket5.items(), key=lambda x: x[1]) if bucket5 else (None, 0)

    bid1 = snap.get("bid_price_1", 0) if snap else 0
    ask1 = snap.get("ask_price_1", 0) if snap else 0
    bid_qty1 = snap.get("bid_qty_1", 0) if snap else 0
    ask_qty1 = snap.get("ask_qty_1", 0) if snap else 0
    bid_total_5 = snap.get("bid_total_5", 0) if snap else 0
    ask_total_5 = snap.get("ask_total_5", 0) if snap else 0
    spread = ask1 - bid1 if (ask1 and bid1) else 0
    spread_pct = (spread / bid1 * 100) if bid1 else 0

    return {
        "_group": "A_ticks_5snap",
        "open": opens, "high": highs, "low": lows, "close": last,
        "amplitude_pct": round(amp, 2),
        "total_volume": total_vol,
        "total_amount": total_amt,
        "buy_ticks_count": buy_count,
        "sell_ticks_count": sell_count,
        "ob_ratio": round(ob_ratio, 3),
        "big_buy_count": big_buy_count,
        "big_sell_count": big_sell_count,
        "big_buy_net_vol": big_buy_net_vol,
        "buy_median_vol": round(buy_median, 1),
        "sell_median_vol": round(sell_median, 1),
        "max_buy_lots": max_buy,
        "max_buy_price": max_buy_price,
        "max_sell_lots": max_sell,
        "max_sell_price": max_sell_price,
        "late_session_signal": late_signal,
        "late_big_buy_vol": late_big_buy_vol,
        "late_big_sell_vol": late_big_sell_vol,
        "max_1min_change_pct": round(max_1min_change, 2),
        "max_1min_time": max_up[0] if max_up[0] else "",
        "peak_5min_bucket": peak_5min[0] or "",
        "peak_5min_volume": peak_5min[1],
        "spread": spread,
        "spread_pct": round(spread_pct, 3),
        "bid_qty1_minus_ask_qty1": bid_qty1 - ask_qty1,
        "bid5_minus_ask5": bid_total_5 - ask_total_5,
    }


def calc_institutional_indicators(inst: Optional[dict]) -> dict:
    """【組 B】三大法人 6 項指標(全市場金額,不是個股)"""
    if not inst:
        return {"_error": "no institutional data", "_source_failed": True}

    foreign = inst.get("外國及陸資") or inst.get("外資及陸資") or {}
    trust = inst.get("投信", {})
    dealer = inst.get("自營商", {})

    foreign_net = foreign.get("net", 0)
    trust_net = trust.get("net", 0)
    dealer_net = dealer.get("net", 0)
    total_net = foreign_net + trust_net + dealer_net
    same_dir = (foreign_net > 0 and trust_net > 0) or (foreign_net < 0 and trust_net < 0)

    return {
        "_group": "B_institutional",
        "_data_scope": "全市場(不是個股)",
        "foreign_net_amount": foreign_net,
        "invest_trust_net_amount": trust_net,
        "dealer_net_amount": dealer_net,
        "total_3instit_net_amount": total_net,
        "foreign_trust_same_direction": same_dir,
        "foreign_trust_combined": foreign_net + trust_net,
    }


def calc_margin_indicators(ms: Optional[dict]) -> dict:
    """【組 C】融資券 6 項指標"""
    if not ms:
        return {"_error": "no margin data", "_source_failed": True}

    margin_change = ms.get("margin_change", 0)
    short_change = ms.get("short_change", 0)
    margin_bal = ms.get("margin_balance", 0)
    short_bal = ms.get("short_balance", 0)
    ratio = ms.get("short_margin_ratio", (short_bal / margin_bal * 100 if margin_bal > 0 else 0))

    retail_sentiment = (
        "偏多延續" if margin_change > 0 and short_change < 0
        else "看空增加" if short_change > 0
        else "中性"
    )

    return {
        "_group": "C_margin_short",
        "margin_balance": margin_bal,
        "margin_change": margin_change,
        "short_balance": short_bal,
        "short_change": short_change,
        "short_margin_ratio_pct": round(ratio, 2),
        "retail_sentiment": retail_sentiment,
    }


def calc_market_index_indicators(market: Optional[dict], stock_close: float = 0, stock_chg_pct: float = 0) -> dict:
    """【組 D】大盤指數 6 項指標"""
    if not market:
        return {"_error": "no market data", "_source_failed": True}

    tse_close = market.get("taiex_close", 0)
    tse_change = market.get("taiex_change", 0)
    tse_prev_close = tse_close - tse_change if tse_close else 0
    tse_chg_pct = (tse_change / tse_prev_close * 100) if tse_prev_close else 0

    sync = (
        "同漲" if stock_chg_pct > 0 and tse_chg_pct > 0
        else "同跌" if stock_chg_pct < 0 and tse_chg_pct < 0
        else "背離" if abs(stock_chg_pct - tse_chg_pct) > 1
        else "同步震盪"
    )

    return {
        "_group": "D_market_index",
        "taiex_close": tse_close,
        "taiex_change": tse_change,
        "taiex_change_pct": round(tse_chg_pct, 2),
        "taiex_volume": market.get("taiex_volume", 0),
        "taiex_trade_value": market.get("taiex_trade_value", 0),
        "sync_with_market": sync,
    }


def calc_foreign_oi_indicators(oi_data: Optional[dict]) -> dict:
    """【組 E】外資未平倉(TXF)指標。目前無穩定免費資料源,calc_all() 未串接,保留供未來擴充。"""
    if not oi_data:
        return {"_error": "no TXF OI data", "_source_failed": True}

    foreign = oi_data.get("外資", {})
    trust = oi_data.get("投信", {})
    dealer = oi_data.get("自營商", {})

    f_net = foreign.get("net_oi", 0)
    f_chg = foreign.get("change_net", 0)

    if f_net > 0 and f_chg > 0:
        signal = "偏多"
    elif f_net < 0 and f_chg < 0:
        signal = "偏空"
    elif f_chg > 1000:
        signal = "翻多"
    elif f_chg < -1000:
        signal = "翻空"
    else:
        signal = "中性"

    return {
        "_group": "E_foreign_oi",
        "txf_foreign_net_oi": f_net,
        "txf_foreign_change": f_chg,
        "txf_trust_net_oi": trust.get("net_oi", 0),
        "txf_dealer_net_oi": dealer.get("net_oi", 0),
        "txf_signal": signal,
    }


def calc_all(ticks: list[dict], snap: dict,
             inst: Optional[dict], ms: Optional[dict],
             market: Optional[dict], symbol: str) -> dict:
    """一次算完組 A/B/C/D 指標,回傳分組 dict。"""
    stock_close = snap.get("close", 0) if snap else 0
    stock_chg_pct = 0
    if ticks and stock_close:
        open_price = ticks[0]["close"]
        if open_price:
            stock_chg_pct = round((stock_close - open_price) / open_price * 100, 2)

    indicators = {
        "symbol": symbol,
        "stock_change_pct": stock_chg_pct,
        "group_A_ticks_5snap": calc_tick_indicators(ticks, snap),
        "group_B_institutional": calc_institutional_indicators(inst),
        "group_C_margin_short": calc_margin_indicators(ms),
        "group_D_market_index": calc_market_index_indicators(market, stock_close, stock_chg_pct),
    }

    d = indicators["group_D_market_index"]
    if not d.get("_source_failed"):
        indicators["relative_strength_pct"] = round(stock_chg_pct - d.get("taiex_change_pct", 0), 2)

    return indicators


if __name__ == "__main__":
    import json
    import sys
    from datetime import date as _date

    from data.ticks import load_ticks_from_db, load_snapshot_from_db
    from data.twse import load_margin_short, load_institutional
    from data.market import load_market

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if not config.symbols:
        print("watchlist 沒有任何標的")
        sys.exit(1)
    symbol = config.symbols[0]
    target = sys.argv[1] if len(sys.argv) > 1 else _date.today().isoformat()
    print(f"=== 測試 {symbol} {target} ===")
    ticks = load_ticks_from_db(symbol, target)
    snap = load_snapshot_from_db(symbol, target)
    inst = load_institutional(target)
    ms = load_margin_short(symbol, target)
    market = load_market(target)
    print(f"ticks={len(ticks)} snap={bool(snap)} inst={bool(inst)} ms={bool(ms)} market={bool(market)}")
    print(json.dumps(calc_all(ticks, snap, inst, ms, market, symbol), ensure_ascii=False, indent=2))
