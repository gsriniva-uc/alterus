"""
retriever.py
Searches the Chroma corpus for relevant context.
Uses Voyage AI embeddings via CorpusStore.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
from ingest.embedder import CorpusStore

CHROMA_DIR = Path(os.getenv("DATA_DIR", "data")) / "chroma_db"

class Retriever:
    def __init__(self, user_id: str = "default"):
        self.store = CorpusStore(CHROMA_DIR, user_id=user_id)

    def search(self, query: str, top_k: int = 5) -> list:
        return self.store.search(query, top_k=top_k)

    def multi_search(self, queries: list, top_k: int = 3) -> list:
        results = []
        seen = set()
        for query in queries:
            for r in self.search(query, top_k=top_k):
                key = r["text"][:50]
                if key not in seen:
                    seen.add(key)
                    results.append(r)
        return results[:top_k * 2]
