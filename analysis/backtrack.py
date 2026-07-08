"""
軍師系統 — 盤後分析主程式 (analysis/backtrack.py)
17:00 觸發,流程:
  1. 抓 ticks + 五檔(Shioaji)
  2. 抓三大法人 + 融資券(TWSE)
  3. 算指標(data/indicators.py)
  4. 書庫 RAG 查詢
  5. 餵 LLM 解讀(經 Claude Code Router)→ 拿分析
  6. 推 Telegram 短報
  7. 存 md 報告

設計:
- 獨立 cron 觸發,不依賴 sentinel
- 任何 fetch 失敗不中斷,標記 ⚠️ 繼續跑
- 標的一律讀 config/watchlist.json,不寫死代號;預設對所有監控股跑一輪
"""
import json
import logging
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from config import REPORTS_DIR, ROOT, config
from llm.rag import rag_query as _rag_query_raw
from llm.router_client import ask_backtrack
from notify.herald import safe_send

log = logging.getLogger("counselor.backtrack")


# ================== 書庫 RAG ==================
def rag_query(question: str, top_k: int = 3) -> list[str]:
    """查 ebook-library,回傳 top_k 段書庫內容(用現成的 query.py 子程序)。"""
    ebook_dir = ROOT.parent.parent / "ebook-library"
    try:
        result = subprocess.run(
            ["python3", "query.py", question],
            cwd=str(ebook_dir),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning(f"RAG query 失敗: {result.stderr[:200]}")
            return []
        chunks = [c.strip() for c in result.stdout.split("---") if c.strip()]
        return chunks[:top_k]
    except Exception as e:
        log.warning(f"RAG 查詢例外: {e}")
        return []


# ================== Prompt 組裝 ==================
SYSTEM_PROMPT = """你是「台股軍師」,擁有豐富的台股籌碼與主力行為判讀經驗。

你的任務:基於當日 ticks、五檔、三大法人、融資券的具體數字,產出盤後分析。

【鐵律】
1. 所有結論必須引用下方數據的具體數字,不可空泛(如不可寫「主力買盤強」,要寫「+50 張大單買進 28 筆,賣出 5 筆」)
2. 預判要給具體價位/張數/百分比
3. 風險要算具體張數或金額
4. 語氣精準俐落,不用客套話
5. 結論放最前(倒金字塔)

【輸出格式】
========================================
## 結論
(2-3 句,最強訊號一句話)

## 主力行為(組 A:ticks+五檔)
- 大單:+50 張買/賣各 X 筆,淨額 X 張
- 尾盤訊號:大單方向「買/賣/中性」
- 內外盤比:X
- 收盤五檔:買賣價差 X 元(0.X%),五檔量差 X 張(買方 X / 賣方 X)
- 量能尖峰:HH:MM 桶 5 分鐘量 X 張

## 法人動向(組 B:三大法人)
- 外資:X 股(金額約 X 萬)
- 投信:X 股
- 自營商:X 股
- 合計:X 股
- 訊號:外資+投信同向「是/否」

## 散戶情緒(組 C:融資券)
- 融資:X 張(增減 +X)
- 融券:X 張(增減 +X)
- 券資比:X%
- 解讀:「偏多延續 / 看空增加 / 中性」

## 交叉綜合(最關鍵)
(2-3 段,主力+法人+散戶三方印證或背離)

## 訊號強度評分
⭐(1-5 顆,代表多空訊號清晰度)

## 明天觀察重點
(2-3 項具體可量化條件)
========================================
"""


def build_user_prompt(symbol: str, symbol_name: str, indicators: dict, rag_chunks: list[str], target_date: str = "") -> str:
    report_date = target_date or date.today().isoformat()
    parts = [f"# {symbol}({symbol_name}) {report_date} 盤後分析\n"]

    parts.append("## 組 A:Ticks + 五檔")
    parts.append(json.dumps(indicators["group_A_ticks_5snap"], ensure_ascii=False, indent=2))

    parts.append("\n## 組 B:三大法人")
    parts.append(json.dumps(indicators["group_B_institutional"], ensure_ascii=False, indent=2))

    parts.append("\n## 組 C:融資券")
    parts.append(json.dumps(indicators["group_C_margin_short"], ensure_ascii=False, indent=2))

    parts.append("\n## 組 D:大盤指數")
    parts.append(json.dumps(indicators["group_D_market_index"], ensure_ascii=False, indent=2))

    if rag_chunks:
        parts.append("\n## 書庫參考(供你引用論述)")
        for i, c in enumerate(rag_chunks, 1):
            parts.append(f"\n### 引用 {i}\n{c[:500]}")

    parts.append("\n\n請依據以上數據,按 SYSTEM 格式產出盤後分析。")
    return "\n".join(parts)


# ================== Telegram 短報 ==================
def make_telegram_summary(symbol: str, symbol_name: str, indicators: dict, llm_output: str) -> str:
    """把 LLM 長分析壓成 8-12 行 Telegram 訊息。"""
    a = indicators["group_A_ticks_5snap"]
    b = indicators["group_B_institutional"]
    c = indicators["group_C_margin_short"]
    d = indicators["group_D_market_index"]
    today = date.today().isoformat()

    has_inst = not b.get("_source_failed")
    has_ms = not c.get("_source_failed")
    has_market = not d.get("_source_failed")

    lines = [f"📊 {symbol} {symbol_name} {today} 盤後", ""]

    conclusion = ""
    for line in llm_output.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and "## 結論" not in line:
            conclusion = line
            break
    lines.append(f"🎯 {conclusion[:80] if conclusion else '數據已收,詳見長報告'}")
    lines.append("")

    lines.append(
        f"主力:+{a['big_buy_count']}大買 / {a['big_sell_count']}大賣 "
        f"淨 {a['big_buy_net_vol']:+,}張,尾盤{a['late_session_signal']}"
    )
    if has_inst:
        f = b['foreign_net_amount'] / 1e8
        t = b['invest_trust_net_amount'] / 1e8
        d_net = b['dealer_net_amount'] / 1e8
        tot = b['total_3instit_net_amount'] / 1e8
        lines.append(f"法人(全市場):外 {f:+.2f}億 投 {t:+.2f}億 自 {d_net:+.2f}億 合計 {tot:+.2f}億")
    else:
        lines.append("法人:⚠️ TWSE 抓取失敗")

    if has_ms:
        lines.append(
            f"籌碼:融資 {c['margin_change']:+,} 融券 {c['short_change']:+,} "
            f"券資 {c['short_margin_ratio_pct']:.1f}% → {c['retail_sentiment']}"
        )
    else:
        lines.append("籌碼:⚠️ TWSE 抓取失敗")

    if has_market:
        rs = indicators.get("relative_strength_pct", 0)
        rs_label = f"RS {rs:+.2f}%" if rs else ""
        sync = d.get("sync_with_market", "")
        lines.append(
            f"大盤:加權 {d['taiex_close']} {d['taiex_change']:+} ({d['taiex_change_pct']:+.2f}%) {sync} {rs_label}"
        )

    lines.append(
        f"價量:開 {a['open']} 收 {a['close']} 高 {a['high']} 低 {a['low']} "
        f"振 {a['amplitude_pct']}%,量 {a['total_volume']:,}張"
    )
    lines.append(f"五檔:差 {a['spread_pct']}%,量差 {a['bid5_minus_ask5']:+,}張")
    lines.append("")

    obs = ""
    in_obs = False
    for line in llm_output.split("\n"):
        if "## 明天觀察重點" in line:
            in_obs = True
            continue
        if in_obs:
            l = line.strip()
            if l.startswith("##") or l.startswith("==="):
                break
            if l.startswith("-"):
                obs += l[1:].strip() + " / "
                if len(obs) > 100:
                    break
    lines.append(f"👀 明天觀察:{obs[:120]}" if obs else "📁 完整報告見 reports/")

    return "\n".join(lines)


# ================== md 報告 ==================
def save_md_report(symbol: str, symbol_name: str, llm_output: str, indicators: dict, today: str) -> Path:
    path = REPORTS_DIR / f"{today}_{symbol}.md"
    a = indicators["group_A_ticks_5snap"]
    b = indicators["group_B_institutional"]
    c = indicators["group_C_margin_short"]
    d = indicators["group_D_market_index"]

    content = f"""# {symbol} {symbol_name} 盤後分析報告
**日期:** {today}
**生成時間:** {datetime.now().strftime("%H:%M:%S")}

---

## 數據快照

### 組 A:Ticks + 五檔
| 項目 | 數值 |
|---|---|
| 開/高/低/收 | {a['open']} / {a['high']} / {a['low']} / {a['close']} |
| 振幅 | {a['amplitude_pct']}% |
| 總量 | {a['total_volume']:,} 張 |
| 總成交額 | {a['total_amount']:,} 元 |
| 內外盤比 | {a['ob_ratio']}(買 {a['buy_ticks_count']} / 賣 {a['sell_ticks_count']}) |
| 大單買/賣 | {a['big_buy_count']} / {a['big_sell_count']} 筆 |
| 大單淨額 | {a['big_buy_net_vol']:+,} 張 |
| 最大單筆買 | {a['max_buy_lots']} 張 @ {a['max_buy_price']} |
| 最大單筆賣 | {a['max_sell_lots']} 張 @ {a['max_sell_price']} |
| 尾盤訊號 | {a['late_session_signal']}(買 {a['late_big_buy_vol']} / 賣 {a['late_big_sell_vol']}) |
| 量能尖峰 | {a['peak_5min_bucket']} 桶 {a['peak_5min_volume']:,} 張 |
| 五檔價差 | {a['spread']} 元({a['spread_pct']}%) |
| 五檔量差 | {a['bid5_minus_ask5']:+,} 張(買1 {a['bid_qty1_minus_ask_qty1']:+,}) |

### 組 B:三大法人（全市場金額）
| 法人 | 淨額 |
|---|---|
| 外資 | {b.get('foreign_net_amount', 0) / 1e8:+.2f} 億 |
| 投信 | {b.get('invest_trust_net_amount', 0) / 1e8:+.2f} 億 |
| 自營商 | {b.get('dealer_net_amount', 0) / 1e8:+.2f} 億 |
| **合計** | **{b.get('total_3instit_net_amount', 0) / 1e8:+.2f} 億** |
| 外資+投信同向 | {b.get('foreign_trust_same_direction', '?')} |

### 組 C:融資券
| 項目 | 數值 |
|---|---|
| 融資餘額 | {c.get('margin_balance', 0):,} 張 |
| 融資增減 | {c.get('margin_change', 0):+,} |
| 融券餘額 | {c.get('short_balance', 0):,} 張 |
| 融券增減 | {c.get('short_change', 0):+,} |
| 券資比 | {c.get('short_margin_ratio_pct', 0):.2f}% |
| 散戶情緒 | {c.get('retail_sentiment', '?')} |

### 組 D:大盤指數
| 項目 | 數值 |
|---|---|
| 加權收盤 | {d.get('taiex_close', 0)} |
| 漲跌點 | {d.get('taiex_change', 0):+} |
| 漲跌幅 | {d.get('taiex_change_pct', 0):+.2f}% |
| 成交量 | {d.get('taiex_volume', 0):,} 億 |
| 成交值 | {d.get('taiex_trade_value', 0):,} 億 |
| 個股與大盤 | {d.get('sync_with_market', '?')} |

---

## LLM 分析

{llm_output}

---

*自動生成 by analysis/backtrack.py(cron 17:00)*
"""
    path.write_text(content, encoding="utf-8")
    return path


# ================== 單一標的主流程 ==================
def run(symbol: str, target_date: Optional[str] = None) -> bool:
    if target_date is None:
        target_date = date.today().isoformat()

    stock_cfg = config.get_stock(symbol)
    symbol_name = stock_cfg.get("name", symbol) if stock_cfg else symbol

    log.info(f"=== 盤後分析啟動 {symbol}({symbol_name}) {target_date} ===")

    from data.ticks import fetch_ticks, load_ticks_from_db, load_snapshot_from_db
    tick_data = fetch_ticks(symbol, target_date)
    if not tick_data or tick_data.get("tick_count", 0) == 0:
        log.error("❌ ticks 抓取失敗,中止")
        return False

    ticks = load_ticks_from_db(symbol, target_date)
    snap = load_snapshot_from_db(symbol, target_date)
    log.info(f"  ticks: {len(ticks)} 筆,快照: {'有' if snap else '無'}")

    from data.twse import fetch_institutional_3instit, fetch_margin_short
    inst = fetch_institutional_3instit(target_date)
    ms = fetch_margin_short(symbol, target_date)

    from data.market import fetch_market_index
    market = fetch_market_index(target_date)
    log.info(f"  大盤: {'有' if market else '無'}")

    from data.indicators import calc_all
    indicators = calc_all(ticks, snap, inst, ms, market, symbol)
    log.info("  指標算完:組 A / B / C / D")

    rag_chunks = rag_query(f"盤後分析 {symbol_name} 主力 法人 籌碼", top_k=3)
    log.info(f"  RAG 命中 {len(rag_chunks)} 段")

    user_prompt = build_user_prompt(symbol, symbol_name, indicators, rag_chunks, target_date)
    llm_output = ask_backtrack(user_prompt, SYSTEM_PROMPT)
    log.info(f"  LLM 分析完成({len(llm_output)} 字)")

    summary = make_telegram_summary(symbol, symbol_name, indicators, llm_output)
    safe_send(summary)
    log.info("  Telegram 短報已送")

    md_path = save_md_report(symbol, symbol_name, llm_output, indicators, target_date)
    log.info(f"  md 報告: {md_path}")

    log.info(f"=== {symbol} 盤後分析完成 ===")
    return True


# ================== 全部監控標的 ==================
def run_all(target_date: Optional[str] = None) -> dict[str, bool]:
    """對 config/watchlist.json 內所有標的各跑一輪盤後分析。"""
    results = {}
    for stock in config.stocks:
        symbol = stock["symbol"]
        try:
            results[symbol] = run(symbol, target_date)
        except Exception as e:
            log.error(f"❌ {symbol} 盤後分析例外: {e}", exc_info=True)
            results[symbol] = False
    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    outcomes = run_all(target)
    log.info(f"=== 全部完成:{outcomes} ===")
    sys.exit(0 if all(outcomes.values()) else 1)
