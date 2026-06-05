"""
軍師系統 — LLM 客戶端 (llm_client.py)
封裝 MiniMax API + RAG 書庫檢索,所有 LLM 呼叫統一走這支。

用法:
    from llm_client import ask_strategist
    order = ask_strategist(
        symbol="2883",
        signal="red",
        snapshot={"price": 23.45, "volume_ratio": 1.8, ...}
    )
"""
import os
import logging
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

_ROOT = Path(__file__).parent
load_dotenv(_ROOT / ".env")

log = logging.getLogger("counselor.llm")

# ===== MiniMax 設定 =====
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M3")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
EBOOK_DB_PATH = os.getenv("EBOOK_DB_PATH", str(_ROOT.parent.parent / "ebook-library" / "db"))


# ===== RAG 查詢 =====
def rag_query(question: str, n_results: int = 3) -> list[dict]:
    """從 ebook-library ChromaDB 撈相關書節。

    回傳: [{"source": "書名/章節", "text": "...", "score": 0.85}, ...]
    """
    if not Path(EBOOK_DB_PATH).exists():
        log.warning(f"找不到 RAG 書庫: {EBOOK_DB_PATH}")
        return []
    try:
        import chromadb
        client = chromadb.PersistentClient(path=EBOOK_DB_PATH)
        # 自動挑資料量最大的 collection
        colls = client.list_collections()
        best = max(colls, key=lambda c: c.count()) if colls else None
        if not best:
            return []
        results = best.query(query_texts=[question], n_results=n_results)
        out = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i] if results.get("metadatas") else {}
            out.append({
                "source": meta.get("source", "未知書節"),
                "text": doc[:500],
                "score": 1 - results["distances"][0][i] if results.get("distances") else 0,
            })
        return out
    except Exception as e:
        log.error(f"RAG 查詢失敗: {e}")
        return []


# ===== 軍師總司令 prompt 模板 =====


def _format_trigger_detail(detail: dict) -> str:
    """把 sentinel 回傳的 detail dict 格式化成人讀的 markdown。

    目的:讓 LLM 看到具體數字(筆數/張數/價區/逐筆),而不是空泛的「R1 觸發」。
    """
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


SYSTEM_PROMPT = """你是台股盤中 AI 軍師,協助 Kevin 判斷是否進場。

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
    """呼叫軍師總司令,回傳 60-120 字密令。"""
    if not MINIMAX_API_KEY:
        return "❌ MINIMAX_API_KEY 未設定"

    # 成本防火牆(每日 50 次, 每月 1000 次)
    try:
        from cost_counter import record_call
        cost = record_call(symbol, signal)
        if cost["daily_remaining"] <= 0:
            return "🛑 當日 LLM 額度用盡(50/50),請 Kevin 評估"
        if cost["alert"]:
            log.warning(cost["alert"])
    except Exception as e:
        log.warning(f"成本計數器跳過: {e}")

    # 1. 撈 RAG 書節(同主題 top-3)
    rag_q = f"{symbol} {'主力表態' if signal == 'red' else '冰山牆瓦解' if signal == 'black' else '籌碼'} 進場 風險管理"
    rag_hits = rag_query(rag_q, n_results=3)
    rag_text = "\n".join(f"《{h['source']}》: {h['text'][:200]}" for h in rag_hits) or "(無相關書節)"

    # 2. 拆出 trigger_detail(避免全部 dump 進 prompt 撐爆 token)
    trigger_detail = snapshot.pop("trigger_detail", {})
    detail_text = _format_trigger_detail(trigger_detail) if trigger_detail else "(無觸發明細)"

    # 3. 組 prompt(snapshot 排除掉 trigger_detail 避免重複)
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

    # 3. 呼叫 MiniMax
    try:
        from openai import OpenAI
        client = OpenAI(api_key=MINIMAX_API_KEY, base_url=MINIMAX_BASE_URL)
        resp = client.chat.completions.create(
            model=MINIMAX_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=500,
            temperature=0.3,
            extra_body={"thinking": {"type": "disabled"}},
        )
        raw = (resp.choices[0].message.content or "").strip()
        # 過濾 <<think>>...</<think>> 思考鍵(有時跳過 disable 還是會輸出)
        import re
        cleaned = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL)
        # 如果清理後為空, 抓 raw 最末段
        if not cleaned.strip() and raw:
            cleaned = raw.split("</think>")[-1].strip()
        return cleaned.strip() or "❌ 軍師無回應(可能被思考鏈吃掉 token)"
    except Exception as e:
        log.error(f"MiniMax 呼叫失敗: {e}")
        return f"❌ 軍師 API 失敗: {e}"


if __name__ == "__main__":
    # CLI 測試
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print(ask_strategist(
            "2883", "red",
            {"price": 23.45, "volume_ratio": 1.8, "tick_density": "high", "twap_burst": True}
        ))
    else:
        print("=== 軍師 LLM 客戶端 ===")
        print(f"  Model: {MINIMAX_MODEL}")
        print(f"  Base URL: {MINIMAX_BASE_URL}")
        print(f"  RAG DB: {EBOOK_DB_PATH}")
        print(f"  RAG exists: {Path(EBOOK_DB_PATH).exists()}")
        # 測試 RAG
        hits = rag_query("凱基金 主力 籌碼")
        print(f"\n  RAG 測試: {len(hits)} 命中")
        for h in hits:
            print(f"    - {h['source']} (score={h['score']:.2f})")
