# 台股軍師 — 系統架構說明

> v2.0 模組化重構(2026-07-08)
> 核心變更:JSON 設定驅動、LLM 改走 Claude Code Router、新增 MCP server

---

## 🎯 系統定位

**AI 軍師 ≠ 自動下單機器人**
- 盤中副駕,只做三件事:偵查 → 分析 → 提醒
- 使用者自己看密令、自己按滑鼠下單
- 所有股票代號、策略門檻、系統參數都在 JSON 設定檔,不寫死在程式碼裡

---

## 🧩 套件結構

```
counselor_agent/
├── config.py                # 統一設定層:讀 .env(密鑰)+ config/*.json(股票與門檻)
├── config/
│   ├── watchlist.json        # 監控股票清單 + 盤中時段
│   └── thresholds.json       # R1-R4 策略門檻 / PriceMonitor / 健康檢查 / LLM 額度
│
├── broker/                   # 券商連線(Shioaji 封裝,唯一知道 API 細節的地方)
│   └── shioaji_broker.py
│
├── core/                     # 核心:即時 tick 偵測管線(極簡化,只做這件事)
│   ├── strategies.py          # StrategyDetector(R1-R4)+ CooldownGate,純邏輯可單元測試
│   ├── price_monitor.py       # tick_size() + PriceMonitor
│   └── sentinel.py            # 進程協調:訂閱/派工/watchdog,狀態全收斂在 Sentinel 實例
│
├── llm/                      # LLM 客戶端(經 Claude Code Router)
│   ├── router_client.py       # ask_strategist() / ask_backtrack(),不再直連任何供應商
│   ├── rag.py                 # ChromaDB 書庫檢索
│   └── cost_counter.py        # 每日/每月呼叫次數計數
│
├── notify/                   # 通知(Telegram)
│   └── herald.py               # send_order/send_alert/send_price_alert + 表格安全網(合併原 telegram_safety.py)
│
├── data/                     # 資料抓取 + 純計算
│   ├── ticks.py                # Shioaji 逐筆成交 + 五檔快照 → SQLite
│   ├── twse.py                 # TWSE 三大法人 + 融資融券 → SQLite
│   ├── market.py               # TWSE 加權指數 → SQLite
│   └── indicators.py           # 純計算:組 A/B/C/D 指標(無 IO)
│
├── analysis/
│   └── backtrack.py            # 盤後分析主流程,預設對 watchlist 全部標的各跑一輪
│
├── mcp_server/                # MCP server:讓 LLM client 查詢/控制本系統
│   ├── tools.py                 # 純函式工具實作(查詢/控制/原始資料)
│   └── server.py                # FastMCP 註冊層,stdio transport
│
├── sentinel.py                # 進入點:設定 logging → core.sentinel.Sentinel().run()
├── strategist.py               # 子進程進入點:LLM + Telegram(獨立 GIL)
├── backtest.py                 # 歷史回測,直接用 core.strategies,與 sentinel 解耦
│
├── run_sentinel.sh / run_backtrack.sh / run_mcp_server.sh
├── .mcp.json                   # 讓 Claude Code 自動啟動 MCP server
└── requirements.txt
```

**分層原則**:`core/` 只碰即時 tick 偵測與進程協調,不知道 LLM 或 Telegram 存在(用 lazy import 呼叫 `notify/`,避免子進程沒必要載入)。`data/` 與 `analysis/` 完全不碰 Shioaji 訂閱邏輯。`mcp_server/` 是獨立進程,不進入 sentinel 的即時迴圈,維持與 Shioaji GIL 隔離的原則。

---

## 🔧 設定驅動(不寫死)

| 檔案 | 內容 |
|---|---|
| `.env` | 密鑰:Shioaji / Telegram / Claude Code Router / RAG 路徑 |
| `config/watchlist.json` | 監控股票清單(可任意增減檔數)、盤中時段 |
| `config/thresholds.json` | R1-R4 策略門檻、試撮時段、訊號冷卻秒數、PriceMonitor、健康檢查、LLM 額度、指標大單門檻、log rotation |

`config.py` 提供熱重載(mtime 偵測):人工編輯 JSON 或透過 MCP 工具寫入,sentinel 主迴圈下次 health check(預設 10 秒)就會套用新值,**不需重啟**。

---

## 🔌 Claude Code Router(LLM 層)

原本 `llm_client.py` / `backtrack.py` 各自直連 MiniMax API(重複邏輯、供應商細節散落兩處)。
v2 統一改走本機執行的 **Claude Code Router(CCR)**:

```
sentinel/backtrack → llm/router_client.py → CCR(本機 127.0.0.1:3456)→ Claude
```

- `llm/router_client.py` 只認得 Anthropic Messages API 相容協定,不知道實際供應商是誰
- 換模型 / 換供應商 / 加 fallback,全部在 CCR 的設定檔處理,不用改本專案任何一行程式碼
- 啟動前提:CCR 需先在本機跑起來(`ccr start`),`.env` 指向它的位址

---

## 🛰️ MCP Server(讓 LLM 查詢/控制本系統)

`mcp_server/` 是獨立進程,透過 stdio 暴露以下工具:

**查詢**:`get_watchlist` / `get_config` / `get_snapshot` / `get_indicators` /
`get_recent_alerts` / `get_sentinel_status` / `get_backtrack_report`

**控制**:`add_watchlist_symbol` / `remove_watchlist_symbol` / `trigger_backtrack` /
`update_strategy_threshold` / `update_price_monitor_config` /
`update_health_check_config` / `update_llm_config` / `update_config_path`

**原始資料**:`query_ticks` / `query_institutional` / `query_margin_short` / `query_market_index`

所有控制工具最終都寫回 `config/*.json`,sentinel 熱重載後立即生效。`.mcp.json` 已註冊好啟動指令,Claude Code 會自動連上。

---

## 🏛️ 四大流派分工(不變)

| 層級 | 流派 | 角色 |
|---|---|---|
| 主軸 | 🟢 趨勢動能 | Stage 2 + VCP 突破訊號 |
| 主軸 | 🟡 籌碼面 | TWAP / 攔截單 / 法人連買 |
| 輔助 | 🔵 價值 | 基本面驗證 |
| 輔助 | 🔴 量化 | 風險管理 |

---

## 🎯 觸發策略(門檻見 `config/thresholds.json`)

| 規則 | 條件形狀 |
|---|---|
| R1 | 短窗口內 ≥N 筆 ≥M 張買單 |
| R2 | 較長窗口,門檻更高 |
| R3 | 窗口內淨買市值超過門檻,同 symbol 有冷卻 |
| R4 | 大單買賣計分,累積超過門檻觸發 |

---

## 📡 PriceMonitor baseline 機制

每 `price_monitor.interval_sec` 秒比對成交價,變動達 `price_monitor.alert_ticks` 檔(TWSE 升降單位)才推 Telegram(不走 LLM)。
基準價只在觸發 alert 時更新,平時沿用「上次 alert 的價」,避免被雜訊洗掉。

---

## 🛡️ 成本防火牆

`config/thresholds.json` 的 `llm` 區塊控制:`daily_call_limit`(每日上限)、`monthly_call_alert`(每月警戒)、`max_tokens_realtime` / `max_tokens_backtrack`。`llm/cost_counter.py` 統一計數,即時盤中訊號與盤後分析都會計入同一組額度。

---

## 🚦 執行方式

```bash
# 盤中監控
./run_sentinel.sh

# 盤後分析(對 watchlist 全部標的各跑一輪)
./run_backtrack.sh

# MCP server(供 LLM client 查詢/控制)
./run_mcp_server.sh

# 回測
python backtest.py

# 單元測試(不連線任何外部服務)
python tests/test_strategies.py
```
