"""
Telegram 訊息安全網 (telegram_safety.py)
=====================================
強制把 Markdown 表格轉成清單,避免 Telegram 跑版。

設計:
  1. 偵測表格(有 |---| 分隔線)
  2. 自動轉成「key — value」一行式清單
  3. 程式層強制,我不會有犯規機會
"""
import re
import logging

log = logging.getLogger("counselor.telegram_safety")


def has_markdown_table(text: str) -> bool:
    """偵測訊息是否含 Markdown 表格(Telegram 跑版兇手)。"""
    # 規則:連續 2+ 行,第二行是 |---| 之類的分隔線
    lines = text.split("\n")
    for i in range(len(lines) - 1):
        if re.match(r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1]):
            return True
    return False


def strip_markdown_table(text: str) -> str:
    """把 Markdown 表格轉成清單語法。

    輸入:
        | 規則 | 等級 | 內容 |
        |---|---|---|
        | T1 | 鐵律 | 禁用表格 |

    輸出:
        • 規則 T1 — 等級 鐵律 — 內容 禁用表格
    """
    lines = text.split("\n")
    out = []
    in_table = False
    header = None

    for line in lines:
        if re.match(r"^\s*\|[\s\-:|]+\|\s*$", line):
            # 分隔線 → 進入表格模式,跳過
            in_table = True
            continue
        if line.lstrip().startswith("|"):
            # 表格行
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if header is None:
                # 第一列(被誤當 header,因為分隔線前沒有第一行?)
                # 處理規則:如果有上一行是 |...| 就當 header
                if out and out[-1].lstrip().startswith("|"):
                    header = [c.strip() for c in out[-1].strip().strip("|").split("|")]
                    out.pop()  # 移除那行(已變成 header)
                else:
                    header = cells
                    continue
            # 把 cell 變成 "header: value" 格式
            pairs = []
            for h, v in zip(header, cells):
                if h and v and h != v:  # 過濾空值與表頭本身
                    pairs.append(f"{h} {v}")
            if pairs:
                out.append("• " + " / ".join(pairs))
        else:
            # 非表格行
            in_table = False
            header = None
            out.append(line)

    return "\n".join(out)


def safe_send(msg: str) -> bool:
    """送 Telegram 前強制安全檢查(取代直接用 herald.send)。

    行為:
      1. 偵測表格 → 自動轉清單(留 header 加註解)
      2. log warning 提醒本次轉換了幾行
      3. 呼叫 herald.send 送出(parse_mode=純文字,避免 * _ 等被誤解)
    """
    from herald import send

    if has_markdown_table(msg):
        original_lines = msg.count("\n")
        msg = strip_markdown_table(msg)
        converted_lines = msg.count("\n")
        log.warning(
            f"⚠️ Telegram 訊息含 Markdown 表格,已自動轉清單 "
            f"({original_lines} 行 → {converted_lines} 行)"
        )
    # 改用 parse_mode=None 純文字,避免 * _ [ ] 等被 Markdown 誤解
    return send(msg, parse_mode=None)


if __name__ == "__main__":
    # 自我測試
    test_msg = """結論先給你:測試

修法摘要:

| 規則 | 等級 | 內容 |
|---|---|---|
| T1 | 鐵律 | 禁用表格 |
| T2 | 鐵律 | App 也別送 |
| T3 | 鐵律 | 自我檢查必跑 |

晚安。
"""
    print("=== 原始(含表格) ===")
    print(test_msg)
    print(f"has_markdown_table: {has_markdown_table(test_msg)}")
    print()
    print("=== 轉換後 ===")
    print(strip_markdown_table(test_msg))
    print()
    print("=== 再偵測一次 ===")
    converted = strip_markdown_table(test_msg)
    print(f"has_markdown_table(轉換後): {has_markdown_table(converted)}")
    assert not has_markdown_table(converted), "轉換後還有表格!"
    print("✅ 通過,轉換後 0 個表格")
