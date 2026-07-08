"""
軍師系統 — RAG 書庫檢索 (llm/rag.py)
從 ebook-library ChromaDB 撈相關書節,供 llm/router_client.py 組 prompt 用。
"""
import logging
from pathlib import Path

from config import EBOOK_DB_PATH

log = logging.getLogger("counselor.rag")


def rag_query(question: str, n_results: int = 3) -> list[dict]:
    """回傳: [{"source": "書名/章節", "text": "...", "score": 0.85}, ...]"""
    if not Path(EBOOK_DB_PATH).exists():
        log.warning(f"找不到 RAG 書庫: {EBOOK_DB_PATH}")
        return []
    try:
        import chromadb
        client = chromadb.PersistentClient(path=EBOOK_DB_PATH)
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
