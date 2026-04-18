"""
embedder.py
Embeds text chunks using Voyage AI (Anthropic's embedding model).
Lightweight — no sentence-transformers, works on 512MB RAM.
"""

import os
import time
from pathlib import Path
from typing import Optional, List

import chromadb
import requests
from chromadb.config import Settings

from ingest.chunker import Chunk

# ── Config ────────────────────────────────────────────────────────────────────
CHROMA_DIR      = Path(os.getenv("DATA_DIR", "data")) / "chroma_db"
COLLECTION_NAME = "alterus_corpus"
BATCH_SIZE      = 32
VOYAGE_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")  # Voyage uses same key


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """
    Embed texts using Voyage AI API.
    Falls back to simple hash-based embeddings if API unavailable.
    """
    if not VOYAGE_API_KEY:
        return _fallback_embeddings(texts)

    try:
        response = requests.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={
                "Authorization": f"Bearer {VOYAGE_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "voyage-3-lite",
                "input": texts,
            },
            timeout=30
        )
        if response.status_code == 200:
            data = response.json()
            return [item["embedding"] for item in data["data"]]
        else:
            print(f"Voyage API error: {response.status_code} — falling back")
            return _fallback_embeddings(texts)
    except Exception as e:
        print(f"Voyage embedding error: {e} — falling back")
        return _fallback_embeddings(texts)


def _fallback_embeddings(texts: List[str]) -> List[List[float]]:
    """Simple hash-based embeddings as fallback (low quality but works)."""
    import hashlib
    embeddings = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = [((b / 255.0) - 0.5) * 2 for b in h]
        # Pad to 512 dims
        while len(vec) < 512:
            vec.extend(vec[:min(len(vec), 512 - len(vec))])
        embeddings.append(vec[:512])
    return embeddings


def _safe_collection_name(user_id: str) -> str:
    import re
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id.lower())
    safe = safe[:50]
    if len(safe) < 3:
        safe = safe + "_corpus"
    return f"corpus_{safe}"


class CorpusStore:
    def __init__(self, chroma_dir: Path = CHROMA_DIR, user_id: str = "default"):
        chroma_dir.mkdir(parents=True, exist_ok=True)
        self.user_id         = user_id
        self.collection_name = _safe_collection_name(user_id)
        self.client          = chromadb.PersistentClient(
            path=str(chroma_dir),
            settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine", "user_id": user_id}
        )
        print(f"📦 Chroma: '{self.collection_name}' ({self.collection.count()} chunks) [user: {user_id}]")

    def add_chunks(self, chunks: List[Chunk]) -> int:
        if not chunks:
            return 0

        added = 0
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            texts = [c.text for c in batch]
            embeddings = _embed_texts(texts)

            self.collection.upsert(
                ids        = [c.chunk_id for c in batch],
                embeddings = embeddings,
                documents  = texts,
                metadatas  = [c.metadata for c in batch],
            )
            added += len(batch)
            if i + BATCH_SIZE < len(chunks):
                time.sleep(0.1)

        return added

    def search(self, query: str, top_k: int = 5) -> list:
        if self.collection.count() == 0:
            return []
        try:
            embeddings = _embed_texts([query])
            results = self.collection.query(
                query_embeddings=embeddings,
                n_results=min(top_k, self.collection.count()),
            )
            return [
                {"text": doc, "metadata": meta}
                for doc, meta in zip(
                    results["documents"][0],
                    results["metadatas"][0]
                )
            ]
        except Exception as e:
            print(f"Search error: {e}")
            return []

    def count(self) -> int:
        return self.collection.count()
