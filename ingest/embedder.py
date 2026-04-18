"""
embedder.py
Embeds text chunks and stores them in Chroma.

Uses sentence-transformers (works everywhere — Railway, MacBook, cloud).
No Ollama needed. Falls back gracefully if model unavailable.
"""

import os
import time
from pathlib import Path
from typing import Optional

import chromadb
import requests
from chromadb.config import Settings

from ingest.chunker import Chunk

# ── Config ────────────────────────────────────────────────────────────────────
CHROMA_DIR      = Path(os.getenv("DATA_DIR", "data")) / "chroma_db"
COLLECTION_NAME = "alterus_corpus"
EMBED_MODEL     = "all-MiniLM-L6-v2"   # fast, good quality, 384 dims
BATCH_SIZE      = 32

# Keep model in memory across calls
_embedding_model = None


def _get_model():
    """Load sentence-transformers model (cached after first load)."""
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            print(f"   📦 Loading embedding model: {EMBED_MODEL}...")
            _embedding_model = SentenceTransformer(EMBED_MODEL)
            print(f"   ✅ Embedding model loaded")
        except Exception as e:
            print(f"   ❌ Could not load embedding model: {e}")
            return None
    return _embedding_model


def embed_text(text: str) -> Optional[list[float]]:
    """Embed a single text string. Returns vector or None on failure."""
    model = _get_model()
    if model is None:
        return None
    try:
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()
    except Exception as e:
        print(f"   ⚠️  Embedding failed: {e}")
        return None


def embed_batch(texts: list[str]) -> list[Optional[list[float]]]:
    """Embed multiple texts at once (faster than one at a time)."""
    model = _get_model()
    if model is None:
        return [None] * len(texts)
    try:
        vectors = model.encode(texts, normalize_embeddings=True, batch_size=BATCH_SIZE)
        return [v.tolist() for v in vectors]
    except Exception as e:
        print(f"   ⚠️  Batch embedding failed: {e}")
        return [None] * len(texts)


def check_ollama() -> bool:
    """
    Compatibility shim — always returns True since we no longer use Ollama.
    Kept so existing code that calls check_ollama() doesn't break.
    """
    model = _get_model()
    if model:
        print(f"✅ Embedding model ready: {EMBED_MODEL}\n")
        return True
    print(f"❌ Embedding model failed to load\n")
    return False


# ── Chroma store ──────────────────────────────────────────────────────────────

def _safe_collection_name(user_id: str) -> str:
    """
    Convert user_id (email or name) to a safe Chroma collection name.
    Chroma requires: 3-63 chars, alphanumeric + underscore/hyphen only.
    """
    import re
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id.lower())
    safe = safe[:50]  # max 50 chars
    if len(safe) < 3:
        safe = safe + "_corpus"
    return f"corpus_{safe}"


class CorpusStore:
    def __init__(self, chroma_dir: Path = CHROMA_DIR, user_id: str = "default"):
        """
        Create a corpus store for a specific user.
        Each user gets their own isolated Chroma collection.
        
        Args:
            chroma_dir: Path to Chroma database
            user_id:    Unique user identifier (email preferred)
                        e.g. "ganesh@gmail.com" or "beta_user_1"
        """
        chroma_dir.mkdir(parents=True, exist_ok=True)
        self.user_id         = user_id
        self.collection_name = _safe_collection_name(user_id)
        self.client          = chromadb.PersistentClient(
            path=str(chroma_dir),
            settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name     = self.collection_name,
            metadata = {"hnsw:space": "cosine", "user_id": user_id}
        )
        print(f"📦 Chroma: '{self.collection_name}' "
              f"({self.collection.count()} chunks) [user: {user_id}]\n")

    def add_chunks(self, chunks: list[Chunk]) -> int:
        """Embed and store chunks. Skips already-stored chunks."""
        if not chunks:
            return 0

        added   = 0
        skipped = 0
        total   = len(chunks)

        print(f"🔢 Embedding {total} chunks...")

        # Process in batches for speed
        for i in range(0, total, BATCH_SIZE):
            batch       = chunks[i : i + BATCH_SIZE]
            batch_texts = [c.text for c in batch]
            batch_ids   = [f"{c.source}__chunk_{c.chunk_idx}" for c in batch]

            # Filter out already-stored chunks
            new_chunks = []
            new_texts  = []
            new_ids    = []
            for chunk, text, cid in zip(batch, batch_texts, batch_ids):
                existing = self.collection.get(ids=[cid])
                if existing["ids"]:
                    skipped += 1
                else:
                    new_chunks.append(chunk)
                    new_texts.append(text)
                    new_ids.append(cid)

            if not new_texts:
                continue

            # Embed the new ones
            vectors = embed_batch(new_texts)

            for chunk, vector, cid in zip(new_chunks, vectors, new_ids):
                if vector is None:
                    continue
                self.collection.add(
                    ids        = [cid],
                    embeddings = [vector],
                    documents  = [chunk.text],
                    metadatas  = [chunk.metadata],
                )
                added += 1

            done = min(i + BATCH_SIZE, total)
            pct  = int(done / total * 100)
            bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
            print(f"   [{bar}] {pct}% ({done}/{total})", end="\r")

        print(f"\n✅ Added: {added} | Skipped: {skipped} | "
              f"Total: {self.collection.count()}\n")
        return added

    def query(
        self,
        query_text: str,
        n_results:  int = 5,
        doc_type:   Optional[str] = None,
    ) -> list[dict]:
        """Semantic search against corpus."""
        vector = embed_text(query_text)
        if vector is None:
            return []

        where = {"doc_type": doc_type} if doc_type else None

        try:
            results = self.collection.query(
                query_embeddings = [vector],
                n_results        = min(n_results, self.collection.count()),
                where            = where,
                include          = ["documents", "metadatas", "distances"],
            )
        except Exception:
            return []

        output = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            output.append({
                "text":       doc,
                "title":      meta.get("title", ""),
                "doc_type":   meta.get("doc_type", ""),
                "source":     meta.get("source", ""),
                "similarity": round(1 - dist, 3),
            })

        return output

    def stats(self) -> dict:
        count = self.collection.count()
        if count == 0:
            return {"total_chunks": 0, "unique_documents": 0}

        all_meta = self.collection.get(include=["metadatas"])["metadatas"]
        doc_types = {}
        sources   = set()
        for m in all_meta:
            dt = m.get("doc_type", "unknown")
            doc_types[dt] = doc_types.get(dt, 0) + 1
            sources.add(m.get("source", ""))

        return {
            "total_chunks":       count,
            "unique_documents":   len(sources),
            "chunks_by_doc_type": doc_types,
        }


if __name__ == "__main__":
    print("Testing embedder...")
    check_ollama()
    store = CorpusStore()
    print(f"Stats: {store.stats()}")
    results = store.query("PRD requirements", n_results=3)
    for r in results:
        print(f"  [{r['similarity']:.3f}] {r['title']}: {r['text'][:80]}...")
