"""
軍師系統 — 通訊兵 (herald.py)
推播軍師密令到 Kevin 的 Telegram Bot。

用法:
    from herald import send_order
    send_order("2883", "🔴 紅色警戒\n【動作】買 ...")
"""
import os
import logging
import urllib.request
import urllib.parse
import json
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

_ROOT = Path(__file__).parent
load_dotenv(_ROOT / ".env")

log = logging.getLogger("counselor.herald")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send(msg: str, parse_mode: str = "Markdown") -> bool:
    """通用 Telegram 推播,回傳是否成功。"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram 設定缺失(TELEGRAM_BOT_TOKEN/CHAT_ID)")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            return result.get("ok", False)
    except Exception as e:
        log.error(f"Telegram 推播失敗: {e}")
        return False


def send_order(symbol: str, order: str, detail: dict | None = None) -> bool:
    """推播軍師密令(會自動加抬頭 + 觸發條件明細)。

    格式:
        🧭 軍師密令 — 2883
        📊 觸發條件明細:
        <精簡版明細>
        <空白行>
        <軍師密令正文>
    """
    header = f"🧭 軍師密令 — {symbol}\n"
    if detail:
        header += "\n📊 觸發條件明細:\n" + _format_detail_compact(detail) + "\n"
    return send(header + order)


def _format_detail_compact(detail: dict) -> str:
    """精簡版觸發明細(給 Telegram 看,行數控制 5-12 行)。"""
    if not detail:
        return "(無)"
    lines = []
    lines.append(f"  規則: {detail.get('rule', '?')} @ {detail.get('triggered_at', '-')}")
    lines.append(f"  成交: 價 {detail.get('price', '?')} / 量 {detail.get('qty', '?')}張 / {detail.get('side', '?')}")
    for rule_key in ("R1", "R2", "R3", "R4"):
        d = detail.get(rule_key)
        if not d:
            continue
        if rule_key in ("R1", "R2"):
            lines.append(
                f"  {rule_key}: {d['count']}筆 (需≥{d['required_count']}) "
                f"/ 總{d['total_lots']}張 / max {d['max_lot']}張 "
                f"/ 價區 {d['price_low']}~{d['price_high']}"
            )
        elif rule_key == "R3":
            ratio = d['buy_sell_ratio'] if d['buy_sell_ratio'] is not None else '∞'
            mv = d['market_value_twd']
            lines.append(
                f"  R3: 買{d['buy_lots']} / 賣{d['sell_lots']} / 淨{d['net_lots']}張 "
                f"(門檻{d['threshold_lots']}) / 比{ratio} / 市值${mv:,.0f}"
            )
        elif rule_key == "R4":
            lines.append(
                f"  R4: counter {d['counter']} (需>{d['required_counter']}) "
                f"/ 買+{d['buy_hits']}次 / 賣-{d['sell_hits']}次"
            )
    return "\n".join(lines)


def send_alert(level: str, msg: str) -> bool:
    """推播系統警示(紅色/黃色/綠色)。"""
    emoji = {"red": "🔴", "yellow": "🟡", "green": "🟢", "black": "⚫"}.get(level, "🔔")
    return send(f"{emoji} {msg}")


def send_price_alert(symbol: str, name: str, prev: float, curr: float, ts: str) -> bool:
    """推播成交價變動通知（每 30 秒 PriceMonitor 呼叫，不走 LLM）。

    Args:
        symbol: 股票代號
        name:   股票名稱
        prev:   上次快照成交價
        curr:   本次成交價
        ts:     時間字串 HH:MM:SS
    """
    diff = curr - prev
    pct  = diff / prev * 100 if prev > 0 else 0
    icon = "📈" if diff > 0 else "📉"
    sign = "+" if diff > 0 else ""
    msg = (
        f"{icon} *{symbol} {name}*  成交價變動\n"
        f"`{prev:.2f}` → `{curr:.2f}`  "
        f"({sign}{diff:.2f} / {sign}{pct:.2f}%)\n"
        f"⏰ {ts}"
    )
    return send(msg)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        ok = send("🧭 軍師通訊兵測試 — Kevin,看到了嗎?")
        print(f"  Telegram 推播: {'✅ 成功' if ok else '❌ 失敗'}")
    else:
        print(f"  Bot token 設定: {'✅' if TELEGRAM_BOT_TOKEN else '❌'}")
        print(f"  Chat ID 設定: {'✅' if TELEGRAM_CHAT_ID else '❌'}")
