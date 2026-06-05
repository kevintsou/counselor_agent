"""
軍師系統 — 軍師子進程 (strategist.py)
專責:LLM 呼叫 + Telegram 推播

子進程透過 multiprocessing.Queue 接收 sentinel 推來的觸發任務,
在獨立 GIL 中執行 LLM 分析與 Telegram 推播,不與 Shioaji 搶鎖。

從父進程接收的 task dict:
{
    "symbol": str,
    "sig": str,       # "R1" / "R2" / "R3" / "R4" / "COMBO"
    "detail": dict,   # 觸發條件明細(窗口/張數/市值等)
    "qty": int,
    "side": str,      # "buy" / "sell" / "unknown"
    "price": float,
    "ts": str,        # ISO 格式時間戳
}
"""
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from multiprocessing import Queue

# 確保可以 import 同目錄的 llm_client / herald
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# 環境變數 + log
from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT, ".env"))

LOG_DIR = os.path.join(_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "strategist.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
    force=True,  # 子進程繼承父進程(sentinel)的 root handlers → basicConfig 預設是 no-op
                 # force=True 強制清掉繼承的 handlers,確保 log 寫進自己的 strategist.log
)
log = logging.getLogger("counselor.strategist")


def graceful_exit(signum, frame):
    """SIGTERM 優雅退出。"""
    log.info(f"🛑 strategist 收到 signal {signum},退出")
    sys.exit(0)


signal.signal(signal.SIGTERM, graceful_exit)
signal.signal(signal.SIGINT, graceful_exit)


def run_forever(task_queue: Queue):
    """主迴圈:從 queue 拿任務,跑 LLM + 推 Telegram。

    失敗一律 catch,絕不 crash(整個 child 死了 sentinel 就不會再有推播)。
    """
    from version import __version__
    log.info("🧠 strategist 子進程啟動 v%s (PID=%d)", __version__, os.getpid())

    # 延遲 import(避免跟 sentinel 同時 load Shioaji)
    try:
        from llm_client import ask_strategist
        from herald import send_order, send_alert
    except Exception as e:
        log.error(f"❌ import 失敗: {e}")
        return

    while True:
        try:
            task = task_queue.get(timeout=10)
        except Exception:
            # queue.Empty 或其他,繼續等
            continue
        if task is None:
            # 毒藥丸:父進程通知退出
            log.info("🛑 收到毒藥丸,退出")
            break

        symbol = task.get("symbol", "?")
        sig = task.get("sig", "?")
        try:
            log.info(f"📥 收到 {symbol} {sig} 任務")
            # 模擬 broker snapshot(子進程沒 Shioaji,只從父進程拿必要欄位)
            snapshot = {
                "qty": task.get("qty", 0),
                "side": task.get("side", ""),
                "price": task.get("price", 0),
                "rule": sig,
                "trigger_detail": task.get("detail", {}),
            }
            order = ask_strategist(symbol, sig.lower(), snapshot)
            log.info(f"  📜 軍師回應 {symbol} ({len(order)} 字)")
            sent = send_order(symbol, order, detail=task.get("detail"))
            log.info(f"  📤 Telegram 推播 {'✅' if sent else '❌'}")
        except Exception as e:
            log.error(f"❌ 處理 {symbol} {sig} 失敗: {e}")
            log.error(traceback.format_exc())
            # 推一則 alert,讓 Kevin 知道有任務失敗
            try:
                from herald import send_alert
                send_alert("red", f"軍師任務失敗 {symbol} {sig}: {str(e)[:200]}")
            except Exception:
                pass


if __name__ == "__main__":
    # 獨立測試用:從 stdin 讀 task
    import json
    log.info("獨立測試模式:從 stdin 讀 task JSON")
    try:
        from llm_client import ask_strategist
        from herald import send_order
        while True:
            line = sys.stdin.readline().strip()
            if not line:
                time.sleep(1)
                continue
            try:
                task = json.loads(line)
            except json.JSONDecodeError:
                log.warning(f"壞 JSON:{line[:100]}")
                continue
            try:
                snapshot = {
                    "qty": task.get("qty", 0),
                    "side": task.get("side", ""),
                    "price": task.get("price", 0),
                    "rule": task.get("sig", ""),
                    "trigger_detail": task.get("detail", {}),
                }
                order = ask_strategist(task["symbol"], task["sig"].lower(), snapshot)
                print(f"📜 軍師回應 ({len(order)} 字):\n{order}", flush=True)
                sent = send_order(task["symbol"], order, detail=task.get("detail"))
                print(f"📤 Telegram: {'✅' if sent else '❌'}", flush=True)
            except Exception as e:
                log.error(f"❌ {e}")
    except KeyboardInterrupt:
        pass
