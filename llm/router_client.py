"""
軍師系統 — LLM 客戶端 (llm/router_client.py)
==============================================
所有 LLM 呼叫統一走 Claude Code Router(CCR),本機代理進程,對外路由到 Claude。
CCR 提供 Anthropic Messages API 相容介面,這裡只需指向本機端點,不再管理
供應商專屬的 API key / base_url / model 名稱 — 那些是 CCR 的設定範疇。

用法:
    from llm.router_client import ask_strategist, ask_backtrack
    order = ask_strategist(symbol="2883", signal="red", snapshot={...})
"""
import logging
import re

from config import CCR_API_KEY, CCR_BASE_URL, CCR_MODEL, config
from llm.rag import rag_query

log = logging.getLogger("counselor.llm")


def _client():
    from anthropic import Anthropic
    return Anthropic(api_key=CCR_API_KEY, base_url=CCR_BASE_URL)


def _call(system: str, user: str, max_tokens: int, temperature: float) -> str:
    """打一次 Claude Code Router,回傳純文字回覆(已過濾 <think> 區塊)。"""
    try:
        client = _client()
        resp = client.messages.create(
            model=CCR_MODEL,
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        raw = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()
        cleaned = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL)
        if not cleaned.strip() and raw:
            cleaned = raw.split("</think>")[-1].strip()
        return cleaned.strip() or "❌ 軍師無回應"
    except Exception as e:
        log.error(f"Claude Code Router 呼叫失敗(base_url={CCR_BASE_URL}): {e}")
        return f"❌ 軍師 API 失敗: {e}"


def _format_trigger_detail(detail: dict) -> str:
    """把 sentinel 回傳的 detail dict 格式化成人讀的 markdown,讓 LLM 看到具體數字。"""
    if not detail:
        return "(無明細)"
    lines = []
    rule = detail.get("rule", "?")
    lines.append(f"  觸發規則: {rule}")
    lines.append(f"  觸發時間: {detail.get('triggered_at', '-')}")
    lines.append(f"  當下成交: 價 {detail.get('price', '?')} / 量 {detail.get('qty', '?')} 張 / 買賣 {detail.get('side', '?')}")
    lines.append("")

    thresholds = detail.get("thresholds", {})
    if thresholds:
        lines.append("  規則門檻(參考):")
        for k, v in thresholds.items():
            lines.append(f"    {k}: {v}")
        lines.append("")

    for rule_key in ("R1", "R2", "R3", "R4"):
        d = detail.get(rule_key)
        if not d:
            continue
        lines.append(f"  ── {rule_key} 命中 ──")
        if rule_key in ("R1", "R2"):
            lines.append(f"    窗口: {d['window_sec']} 秒 / 筆數: {d['count']} (需 ≥{d['required_count']})")
            lines.append(f"    總量: {d['total_lots']} 張 / 最大: {d['max_lot']} / 最小: {d['min_lot']} / 平均: {d['avg_lot']:.1f}")
            lines.append(f"    價區: {d['price_low']} ~ {d['price_high']}")
            tick_summary = ", ".join(f"{t['ts']} {t['qty']}張@{t['price']}" for t in d["ticks"][:8])
            lines.append(f"    逐筆: {tick_summary}{' ...' if len(d['ticks']) > 8 else ''}")
        elif rule_key == "R3":
            lines.append(f"    窗口: {d['window_sec']} 秒 / 門檻: {d['threshold_lots']} 張")
            lines.append(f"    總買: {d['buy_lots']} 張 / 總賣: {d['sell_lots']} 張 / 淨買: {d['net_lots']} 張")
            lines.append(f"    買賣比: {d['buy_sell_ratio']} / 淨買市值: 約 ${d['market_value_twd']:,.0f}")
            buy_summary = ", ".join(f"{t['ts']} {t['qty']}張@{t['price']}" for t in d["buy_ticks"][:6])
            sell_summary = ", ".join(f"{t['ts']} {t['qty']}張@{t['price']}" for t in d["sell_ticks"][:6])
            lines.append(f"    買單: {buy_summary}{' ...' if len(d['buy_ticks']) > 6 else ''}")
            lines.append(f"    賣單: {sell_summary}{' ...' if len(d['sell_ticks']) > 6 else ''}")
        elif rule_key == "R4":
            lines.append(f"    窗口: {d['window_sec']} 秒 / counter: {d['counter']} (需 >{d['required_counter']})")
            lines.append(f"    買盤+次: {d['buy_hits']} / 賣盤-次: {d['sell_hits']}")
            hit_summary = ", ".join(f"{t['ts']} {t['side']} {t['qty']}張" for t in d["ticks"][:8])
            lines.append(f"    顯著單: {hit_summary}{' ...' if len(d['ticks']) > 8 else ''}")
        lines.append("")
    return "\n".join(lines)


SYSTEM_PROMPT_STRATEGIST = """你是台股盤中 AI 軍師,協助 Kevin 判斷是否進場。

【四流派分工】
- 🟢 趨勢動能(主軸):Stage 2 + VCP 突破
- 🟡 籌碼面(主軸):TWAP 抽水機 / 攔截單 / 法人連買
- 🔵 價值(輔助):基本面驗證
- 🔴 量化(輔助):風險管理

【以數據為本(鐵律)】
- 【依據】必須引用「觸發條件明細」中的具體數字,例如:
  • 「R1 五秒內 6 筆 50+ 張買單,最大 78 張,價區 25.4-25.6」✅
  • 「主力買盤強勁」❌ ← 廢話,不接受
- 【失效】必須給出具體價位,例如「跌破 25.00 收盤」,不接受「跌破支撐」
- 【風險】要算具體張數或金額,例如「≤ 2 張 (=5.1 萬)」,不接受「小部位」

【輸出格式(嚴格遵守)】
【動作】買/賣/觀望
【依據】引用觸發明細的數字(R1 筆數/張數、R3 淨買/門檻、R4 counter 等)+ 主力意圖解讀
【風險】單筆 ≤ X 張 / Y 萬元 / Z%
【失效】跌破/突破某具體價位 → 觸發反向動作
(總長 80-180 字,允許引用數字所以放寬)

【風格】冷靜、內行、像資深操盤手,拒絕官腔與廢話。
【書庫知識】以下書節為輔助,取其精華,不要照抄。
"""


def ask_strategist(symbol: str, signal: str, snapshot: dict) -> str:
    """呼叫軍師總司令,回傳 80-180 字密令。"""
    llm_cfg = config.llm_config()

    try:
        from llm.cost_counter import record_call
        cost = record_call(symbol, signal)
        if cost["daily_remaining"] <= 0:
            return f"🛑 當日 LLM 額度用盡({llm_cfg.get('daily_call_limit', 50)}/{llm_cfg.get('daily_call_limit', 50)}),請 Kevin 評估"
        if cost["alert"]:
            log.warning(cost["alert"])
    except Exception as e:
        log.warning(f"成本計數器跳過: {e}")

    rag_q = f"{symbol} {'主力表態' if signal == 'red' else '冰山牆瓦解' if signal == 'black' else '籌碼'} 進場 風險管理"
    rag_hits = rag_query(rag_q, n_results=3)
    rag_text = "\n".join(f"《{h['source']}》: {h['text'][:200]}" for h in rag_hits) or "(無相關書節)"

    trigger_detail = snapshot.pop("trigger_detail", {})
    detail_text = _format_trigger_detail(trigger_detail) if trigger_detail else "(無觸發明細)"

    snap_text = "\n".join(f"  {k}: {v}" for k, v in snapshot.items() if k != "trigger_detail")
    user_msg = f"""標的: {symbol}
訊號等級: {signal.upper()}
觸發規則: {trigger_detail.get('rule', signal.upper())}
觸發時間: {trigger_detail.get('triggered_at', '-')}
最新成交價: {trigger_detail.get('price', snapshot.get('price', '?'))}

【觸發條件明細(必讀,請引用具體數字)】:
{detail_text}

盤面快照:
{snap_text}

相關書節:
{rag_text}

請下密令(依據請引用上述數字):"""

    return _call(
        SYSTEM_PROMPT_STRATEGIST, user_msg,
        max_tokens=llm_cfg.get("max_tokens_realtime", 300),
        temperature=llm_cfg.get("temperature", 0.3),
    )


def ask_backtrack(prompt: str, system: str) -> str:
    """盤後深度分析呼叫(analysis/backtrack.py 用,system prompt 由呼叫端提供)。"""
    llm_cfg = config.llm_config()
    try:
        from llm.cost_counter import record_call
        cost = record_call("backtrack", "daily_report")
        if cost["alert"]:
            log.warning(cost["alert"])
    except Exception as e:
        log.warning(f"成本計數器跳過: {e}")

    return _call(
        system, prompt,
        max_tokens=llm_cfg.get("max_tokens_backtrack", 2000),
        temperature=llm_cfg.get("temperature", 0.3),
    )


if __name__ == "__main__":
    print("=== 軍師 LLM 客戶端(Claude Code Router)===")
    print(f"  CCR base_url: {CCR_BASE_URL}")
    print(f"  CCR model: {CCR_MODEL}")
    hits = rag_query("凱基金 主力 籌碼")
    print(f"\n  RAG 測試: {len(hits)} 命中")
    for h in hits:
        print(f"    - {h['source']} (score={h['score']:.2f})")
