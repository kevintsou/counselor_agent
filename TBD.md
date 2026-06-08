# 軍師系統 — 待修清單 (TBD)

> **接手時間:** 2026-06-08
> **作者:** Fainder 汎德（Kevin 的 AI 助理）
> **對象:** 接手維護軍師系統的工程師

這份文件列出**軍師系統 (counselor_agent) 目前已知需要修的問題**，以及**動手前必讀的背景**。請依優先序處理。

---

## 📂 程式位置

```
/Users/kjkin2006/.openclaw/workspace/projects/counselor_agent/
```

**核心檔案一覽**：

| 檔案 | 用途 | 重要程度 |
|---|---|---|
| `sentinel.py` | **主監控 daemon**（Shioaji 連線 + tick 接收 + PriceMonitor + Watchdog） | 🔴 必讀 |
| `herald.py` | **Telegram 推播**（訊息格式、發送函式） | 🔴 必讀 |
| `ticks_fetcher.py` | 抓 tick 資料 | 🟡 選讀 |
| `broker.py` | Shioaji 連線管理（login/logout/session） | 🟡 選讀 |
| `strategist.py` | 策略推理（走 LLM 觸發 R1-R4） | 🟡 選讀 |
| `indicators.py` | 技術指標計算 | 🟢 參考 |
| `cost_counter.py` | LLM API 成本追蹤 | 🟢 參考 |
| `backtrack.py` | 回測 / 歷史回放 | 🟢 參考 |
| `watchlist.yaml` | 持股清單（symbol / cost / shares） | 🟡 必讀 |
| `.env` | **所有 secret**（Shioaji / LLM / Telegram / 書庫路徑） | 🚫 不可 commit |
| `ARCHITECTURE.md` | 系統架構圖 | 🟡 必讀 |
| `COUNSELOR_MEMORY.md` | 觸發策略 R1-R4 / 試撮過濾 / cooldown / API 陷阱 | 🟡 必讀（已在 .gitignore） |
| `BACKTEST_LOG.md` | 回測紀錄 | 🟢 參考 |
| `logs/` | sentinel / strategist / backtrack 的 log | 🟢 參考 |
| `state/` | 持久化狀態（成本計數、cooldown 等） | 🟢 參考 |

---

## 🚀 啟動 / 停止 / 監看指令

```bash
# 重啟 sentinel（用 launchd kickstart，最安全）
launchctl kickstart -k "gui/$(id -u)/ai.openclaw.counselor-sentinel"

# 監看 log（出問題先看這個）
tail -f /Users/kjkin2006/.openclaw/workspace/projects/counselor_agent/logs/sentinel.log

# keepalive 機制（崩了會自動重啟）
cat ~/Library/LaunchAgents/ai.openclaw.counselor.keepalive.plist
# → 每 60 秒檢查 sentinel 是否活著，死了就重啟

# 看 sentinel PID 是否還在
ps aux | grep sentinel.py | grep -v grep
```

---

## ✅ TBD 1. 同個 Telegram bot 兩用導致 sender label 混淆 [優先序: 中]

### 問題
OpenClaw channel 跟 `herald.py` 共用同一個 bot token（`KAAI_Fainder_Bot`），所以 sentinel 推播的訊息，sender 會被 Telegram 顯示成 Fainder（其實是軍師發的）。

**實際觀察**（2026-06-08 凱基金大跌時）：
```
📉 2883 凱基金 成交價變動 17 檔 27.40 → 26.55 (-0.85 / -3.10%) ⏰ 10:57:46
```
- **實際發送者**: sentinel.py 透過 herald.py 呼叫 Telegram API
- **Telegram 顯示 sender**: KAAI_Fainder_Bot（被當成 Fainder 的訊息）
- **語意錯誤**: 這則是軍師密令，卻標成 Fainder

### 修法
**選項 A（推薦，修乾淨）**: 跟 @BotFather 申請一個新的 Telegram bot（例如 `KAAI_Counselor_Bot`），把新 token 寫進軍師的 `.env`：
```bash
TELEGRAM_BOT_Counselor=NEW_TOKEN  # 新 bot
TELEGRAM_CHAT_ID=8432968775       # Kevin 的 chat_id 不變
```
然後 `herald.py` 改成讀新的 env 變數。

**選項 B（折衷）**: 接受 sender 顯示成 Fainder，但在訊息開頭加「🤖 軍師」prefix，讓視覺上能區分。改 `herald.py` 的 `send_price_alert` 即可。

**選項 C（架構）**: 改讓軍師走 OpenClaw 的 messaging API 推播，讓 OpenClaw routing 處理 sender 顯示。但這需要重構 herald.py。

**注意**: 兩個 bot 還是要送到同一個 chat_id (8432968775)，Kevin 不用換頻道。

### 動手前確認
- [ ] 跟 Kevin 確認要哪個選項（A / B / C）
- [ ] 如果選 A：跟 @BotFather 對話拿新 token，**這個 Kevin 自己做**

---

## ✅ TBD 2. 補 ARCHITECTURE.md 的 PriceMonitor baseline 條目 [優先序: 低]

### 問題
2026-06-08 改了 `sentinel.py` 的 PriceMonitor baseline 機制（觸發後才更新基準、初始快照留空），但**沒補對應的設計意圖到 ARCHITECTURE.md**。COUNSELOR_MEMORY.md 有提到但已 gitignore（不入 repo），所以接手的人看不到完整決策脈絡。

**註**: 此條目已於 2026-06-08 commit 進 sentinel.py（docstring 內），但 ARCHITECTURE.md 的「PriceMonitor 段落」可能還沒同步。接手時請檢查。

### 修法
在 `ARCHITECTURE.md` 的 PriceMonitor 段落加：
```markdown
### PriceMonitor baseline 機制 (2026-06-08 拍板)
- 初始快照留空,等第一筆 tick 來時設為基準(**不推播**)
- 變動 ≥ 4 檔 → 觸發 alert + 把現價寫成新 baseline
- 變動 < 4 檔 → 沿用舊 baseline(不更新,避免被 30 秒雜訊洗版)
- **不要從 watchlist.yaml 的 `cost` 載入**(成本價不是前日收盤,語意不對)
```

### 動手前確認
- [ ] 先讀 ARCHITECTURE.md 看 PriceMonitor 段落現在寫什麼,再決定要補在哪

---

## ✅ TBD 3. 清理孤兒備份檔 [優先序: 低]

### 問題
2026-06-08 baseline 機制改完後，`workspace/scripts/last_alert_price.json` 已改放到 `skills/stock-watcher/`，原本的位置改成 `.bak` 留作保險。

**位置**: `~/.openclaw/workspace/scripts/last_alert_price.json.bak`

### 修法
確認沒被任何 script 讀到後刪除：
```bash
grep -rn "last_alert_price" /Users/kjkin2006/.openclaw/workspace/scripts/ 2>/dev/null
# 確認沒人讀再刪
rm /Users/kjkin2006/.openclaw/workspace/scripts/last_alert_price.json.bak
```

---

## ✅ TBD 4. `sentinel.py` 內的 sentinel.log 沒 log rotation [優先序: 中]

### 問題
`logs/sentinel.log` 已經長到 425 KB（2026-06-08 觀察），沒做 log rotation。長期會吃磁碟。

### 修法
**選項 A**: 在 sentinel.py 啟動時用 Python `logging.handlers.RotatingFileHandler`，單檔 10 MB,保留 5 個備份。
**選項 B**: 用 launchd 把 log 輸出到 `newsyslog` 管理的位置（macOS 原生 log rotation）。
**選項 C**: 寫個 cron job 每天 00:00 清 log。

### 動手前確認
- [ ] 跟 Kevin 確認要哪個選項 + log 保留多久

---

## ✅ TBD 5. keepalive plist 60 秒偵測可能太慢 [優先序: 低]

### 問題
`ai.openclaw.counselor.keepalive.plist` 用 `StartInterval: 60`，意思是最壞情況 sentinel 死了要等 60 秒才被重啟。

### 修法
改成 30 秒,或在 sentinel.py 自己加 `try/except` + 內部 watchdog 偵測。

### 動手前確認
- [ ] 跟 Kevin 確認可接受停機時間

---

## 🚫 不要動的東西

接手前請先讀 `COUNSELOR_MEMORY.md`（雖然不入 repo,Kevin 會私下給你看）。裡面有:

- **觸發策略 R1-R4**（Kevin 拍板的設計邏輯）
- **試撮過濾規則**（避免被測試單洗版）
- **R3 cooldown 機制**（避免短時間重複觸發）
- **API 陷阱**（Shioaji / LLM / Telegram 各家的怪 bug）

改這些前**務必**先跟 Kevin 確認。

另外 **`sentinel.py` 內標記 `# ✅ Kevin 2026-06-08 拍板:` 的段落**是近期拍板的設計決策，要退回請先問 Kevin。

---

## 📞 聯絡

- **主要聯絡人**: Kevin (Telegram: 8432968775)
- **助理聯絡人**: Fainder 汎德 (Kevin 的 AI 助理,透過 Kevin 聯繫)
- **Issue tracker**: 暫無（之後可能用 GitHub Issues）
