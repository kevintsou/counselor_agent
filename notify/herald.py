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

from config import STATE_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, config

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


_ACTION_STYLE = {"買": ("🟢", "買進"), "賣": ("🔴", "賣出"), "觀望": ("⚪", "觀望")}


def _fmt_money(twd) -> str:
    """金額換算億/萬,盤中一眼看懂量級。"""
    v = float(twd or 0)
    if abs(v) >= 1e8:
        return f"約 {v / 1e8:.2f} 億"
    if abs(v) >= 1e4:
        return f"約 {v / 1e4:.0f} 萬"
    return f"${v:,.0f}"


def _parse_order(text: str) -> dict | None:
    """解析軍師四欄輸出(動作/研判/失效/風險)。容錯支援【欄名】與「欄名:」兩種寫法;都認不出回 None。"""
    norm = re.sub(r"【\s*(動作|研判|依據|失效|風險|失效/風險)\s*】", r"\1:", text)
    out = {"動作": "", "研判": "", "失效": "", "風險": ""}
    cur = None
    found = False
    for raw in norm.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(動作|研判|依據|失效|風險|失效/風險)\s*[:：]\s*(.*)$", line)
        if m:
            found = True
            key, val = m.group(1), m.group(2).strip()
            if key == "依據":
                key = "研判"
            if key == "失效/風險":  # 舊格式合併欄:整段塞失效,風險留空
                out["失效"] = val
                cur = "失效"
                continue
            out[key] = val
            cur = key
        elif cur:  # 續行(研判可能多行)
            out[cur] += " " + line
    return out if found else None


def _action_style(action_text: str) -> tuple[str, str]:
    for kw, (emoji, label) in _ACTION_STYLE.items():
        if kw in action_text:
            return emoji, label
    return "🔔", action_text or "研判"


def _format_bidask_compact(ba: dict | None) -> str:
    """Telegram 精簡盤口:最佳買賣一檔 + 五檔總量失衡(完整五檔給 LLM,不塞給人看)。"""
    if not ba:
        return ""
    bp, bv = ba.get("bid_price", []), ba.get("bid_volume", [])
    ap, av = ba.get("ask_price", []), ba.get("ask_volume", [])
    if not (bp and ap):
        return ""
    lines = [f"📖 盤口　委買 {bp[0]:.2f}×{bv[0]} / 委賣 {ap[0]:.2f}×{av[0]}"]
    tot_b, tot_a = sum(bv), sum(av)
    if tot_a > 0:
        imb = tot_b / tot_a
        arrow = "▲偏買" if imb > 1.3 else "▼偏賣" if imb < 0.77 else "◆均衡"
        lines.append(f"　　　委買 {tot_b:,} vs 委賣 {tot_a:,} 張　{imb:.1f} 倍 {arrow}")
    return "\n".join(lines)


def send_order(symbol: str, order: str, detail: dict | None = None, bidask: dict | None = None) -> bool:
    """推播軍師密令。解析四欄 → 結論置頂 + 顏色 + 對齊明細 + 精簡盤口;解析失敗則原文回退。"""
    stock = config.get_stock(symbol) or {}
    label = f"{symbol} {stock.get('name', '')}".strip()
    parsed = _parse_order(order)
    _record_alert("order", symbol, order)

    if not parsed:  # 解析失敗 → 不丟失 LLM 原文,補抬頭與明細後原樣送出
        header = f"🧭 軍師密令 — {label}\n"
        if detail:
            header += "\n📊 觸發明細\n" + _format_detail_compact(detail) + "\n"
        ba = _format_bidask_compact(bidask)
        return send(header + (ba + "\n" if ba else "") + "\n" + order, parse_mode=None)

    emoji, action_label = _action_style(parsed["動作"])
    lines = [f"{emoji} {action_label} · {label}", "━" * 12]
    if detail:
        lines.append(f"{detail.get('triggered_at', '-')}　{detail.get('rule', '?')} 觸發")
        lines.append("")
        lines.append("📊 觸發明細")
        lines.append(_format_detail_compact(detail))
    ba = _format_bidask_compact(bidask)
    if ba:
        lines += ["", ba]
    lines += ["", "🧭 軍師研判", f"　{parsed['研判'] or '(無)'}"]
    if parsed["失效"]:
        lines += ["", f"🎯 失效　{parsed['失效']}"]
    if parsed["風險"]:
        lines.append(f"⚠️ 風險　{parsed['風險']}")
    return send("\n".join(lines), parse_mode=None)


def _format_detail_compact(detail: dict) -> str:
    """精簡觸發明細:兩欄對齊 + 千分位 + 市值億/萬 + 超門檻倍數。"""
    if not detail:
        return "　(無)"
    lines = [f"　成交　價 {detail.get('price', '?')} / 量 {detail.get('qty', '?')} 張 / {detail.get('side', '?')}"]
    for rule_key in ("R1", "R2", "R3", "R4"):
        d = detail.get(rule_key)
        if not d:
            continue
        if rule_key in ("R1", "R2"):
            lines.append(
                f"　{rule_key}　{d['count']} 筆(需≥{d['required_count']})· "
                f"總 {d['total_lots']:,} 張 · 最大 {d['max_lot']:,}"
            )
            lines.append(f"　　　價區 {d['price_low']}~{d['price_high']}")
        elif rule_key == "R3":
            ratio = d['buy_sell_ratio'] if d['buy_sell_ratio'] is not None else '∞'
            over = d['net_lots'] / d['threshold_lots'] if d['threshold_lots'] else 0
            lines.append(f"　R3　淨買 {d['net_lots']:,} 張(門檻 {d['threshold_lots']:.0f},超 {over:.1f} 倍)")
            lines.append(
                f"　　　買 {d['buy_lots']:,} / 賣 {d['sell_lots']:,} · 比 {ratio} · 市值 {_fmt_money(d['market_value_twd'])}"
            )
        elif rule_key == "R4":
            lines.append(
                f"　R4　counter {d['counter']}(需>{d['required_counter']})· "
                f"買+{d['buy_hits']} / 賣-{d['sell_hits']}"
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
