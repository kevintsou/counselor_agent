"""
軍師系統 — 通訊兵 (notify/herald.py)
推播軍師密令到 Kevin 的 Telegram Bot。

用法:
    from notify.herald import send_order
    send_order("2883", "🔴 紅色警戒\\n【動作】買 ...")

safe_send() 用於可能含 Markdown 表格 / 未知特殊字元的長文(如盤後 LLM 分析),
會自動偵測表格轉清單、強制純文字,避免 Telegram 跑版或解析失敗。
"""
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from config import STATE_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger("counselor.herald")

ALERTS_LOG_PATH = STATE_DIR / "alerts.jsonl"
ALERTS_LOG_MAX_LINES = 500


def _record_alert(kind: str, symbol: str, text: str) -> None:
    """把推播事件寫進 state/alerts.jsonl,供 MCP get_recent_alerts 工具讀取。超過上限就截斷保留最新。"""
    try:
        entry = {"ts": datetime.now().isoformat(), "kind": kind, "symbol": symbol, "text": text[:500]}
        lines = []
        if ALERTS_LOG_PATH.exists():
            lines = ALERTS_LOG_PATH.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False))
        if len(lines) > ALERTS_LOG_MAX_LINES:
            lines = lines[-ALERTS_LOG_MAX_LINES:]
        ALERTS_LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        log.debug(f"alerts.jsonl 寫入失敗(不影響推播): {e}")


def send(msg: str, parse_mode: Optional[str] = "Markdown") -> bool:
    """通用 Telegram 推播,回傳是否成功。

    parse_mode=None 或空字串 → 不傳給 Telegram(純文字,不解析 Markdown)。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram 設定缺失(TELEGRAM_BOT_TOKEN/CHAT_ID)")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        params: dict = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "disable_web_page_preview": "true",
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            return result.get("ok", False)
    except Exception as e:
        log.error(f"Telegram 推播失敗: {e}")
        return False


def send_order(symbol: str, order: str, detail: dict | None = None) -> bool:
    """推播軍師密令(會自動加抬頭 + 觸發條件明細)。"""
    header = f"🧭 軍師密令 — {symbol}\n"
    if detail:
        header += "\n📊 觸發條件明細:\n" + _format_detail_compact(detail) + "\n"
    _record_alert("order", symbol, order)
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
    """推播系統警示(紅色/黃色/綠色/黑色)。"""
    emoji = {"red": "🔴", "yellow": "🟡", "green": "🟢", "black": "⚫"}.get(level, "🔔")
    _record_alert(f"system_{level}", "-", msg)
    return send(f"{emoji} {msg}")


def send_price_alert(symbol: str, name: str, prev: float, curr: float,
                     ts: str, ticks_moved: int = 0) -> bool:
    """推播成交價變動通知（PriceMonitor 呼叫，不走 LLM）。"""
    diff = curr - prev
    pct = diff / prev * 100 if prev > 0 else 0
    icon = "📈" if diff > 0 else "📉"
    sign = "+" if diff > 0 else ""
    tick_str = f"  {ticks_moved} 檔" if ticks_moved else ""
    msg = (
        f"{icon} *{symbol} {name}*  成交價變動{tick_str}\n"
        f"`{prev:.2f}` → `{curr:.2f}`  "
        f"({sign}{diff:.2f} / {sign}{pct:.2f}%)\n"
        f"⏰ {ts}"
    )
    _record_alert("price", symbol, f"{prev} -> {curr} ({sign}{pct:.2f}%)")
    return send(msg)


# ===== Markdown 表格安全網(原 telegram_safety.py 合併)=====
def has_markdown_table(text: str) -> bool:
    """偵測訊息是否含 Markdown 表格(Telegram 跑版兇手)。"""
    lines = text.split("\n")
    for i in range(len(lines) - 1):
        if re.match(r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1]):
            return True
    return False


def strip_markdown_table(text: str) -> str:
    """把 Markdown 表格轉成清單語法:「• 欄位 值 / 欄位 值」。"""
    lines = text.split("\n")
    out = []
    header = None

    for line in lines:
        if re.match(r"^\s*\|[\s\-:|]+\|\s*$", line):
            continue  # 分隔線,跳過
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if header is None:
                if out and out[-1].lstrip().startswith("|"):
                    header = [c.strip() for c in out[-1].strip().strip("|").split("|")]
                    out.pop()
                else:
                    header = cells
                    continue
            pairs = []
            for h, v in zip(header, cells):
                if h and v and h != v:
                    pairs.append(f"{h} {v}")
            if pairs:
                out.append("• " + " / ".join(pairs))
        else:
            header = None
            out.append(line)

    return "\n".join(out)


def safe_send(msg: str) -> bool:
    """送 Telegram 前強制安全檢查(給可能含表格/特殊字元的長文,如盤後分析用)。

    1. 偵測表格 → 自動轉清單
    2. 強制 parse_mode=None(純文字),避免 * _ [ ] 等被 Markdown 誤解
    """
    if has_markdown_table(msg):
        original_lines = msg.count("\n")
        msg = strip_markdown_table(msg)
        log.warning(
            f"⚠️ Telegram 訊息含 Markdown 表格,已自動轉清單 "
            f"({original_lines} 行 → {msg.count(chr(10))} 行)"
        )
    return send(msg, parse_mode=None)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        ok = send("🧭 軍師通訊兵測試 — Kevin,看到了嗎?")
        print(f"  Telegram 推播: {'✅ 成功' if ok else '❌ 失敗'}")
    else:
        print(f"  Bot token 設定: {'✅' if TELEGRAM_BOT_TOKEN else '❌'}")
        print(f"  Chat ID 設定: {'✅' if TELEGRAM_CHAT_ID else '❌'}")
