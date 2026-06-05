"""
軍師系統版本資訊
"""

__version__      = "1.0.0"
__release_date__ = "2026-06-06"
__description__  = "台股軍師 — 即時 tick 監控 + LLM 分析 + Telegram 推播"
__author__       = "Kevin Tsou"

# 語意化版本說明
# 1.x.x — 盤中監控核心穩定版（multiprocessing 架構、Shioaji 1.5+）
# 進版規則:
#   MAJOR: 架構大改（換 broker / 換 LLM provider）
#   MINOR: 新功能（新策略規則、新 watchlist 支援）
#   PATCH: bug 修正、效能優化
