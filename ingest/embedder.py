"""
embedder.py
Embeds text chunks using Ollama nomic-embed-text (fully local, no API cost)
and stores them in a local Chroma vector database.
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
CHROMA_DIR      = Path("data/chroma_db")
COLLECTION_NAME = "ganesh_corpus"
OLLAMA_URL      = "http://localhost:11434/api/embeddings"
EMBED_MODEL     = "nomic-embed-text"
BATCH_SIZE      = 10    # embed N chunks at a time


# ── Ollama embedding ──────────────────────────────────────────────────────────

def embed_text(text: str) -> Optional[list[float]]:
    """Call Ollama to embed a single text. Returns vector or None on failure."""
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception as e:
        print(f"   ⚠️  Embedding failed: {e}")
        return None


def check_ollama() -> bool:
    """Verify Ollama is running and nomic-embed-text is available."""
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        if not any("nomic-embed-text" in m for m in models):
            print("❌ nomic-embed-text not found. Run: ollama pull nomic-embed-text")
            return False
        print("✅ Ollama running with nomic-embed-text\n")
        return True
    except Exception:
        print("❌ Ollama not running. Start it with: ollama serve")
        return False


# ── Chroma store ─────────────────────────────────────────────────────────────

class CorpusStore:
    def __init__(self, chroma_dir: Path = CHROMA_DIR):
        chroma_dir.mkdir(parents=True, exist_ok=True)
        self.client     = chromadb.PersistentClient(
            path=str(chroma_dir),
            settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}
        )
        print(f"📦 Chroma collection: '{COLLECTION_NAME}' "
              f"({self.collection.count()} docs existing)\n")

    def add_chunks(self, chunks: list[Chunk]) -> int:
        """Embed and store chunks. Skips already-stored chunks."""
        added   = 0
        skipped = 0
        total   = len(chunks)

        print(f"🔢 Embedding {total} chunks with {EMBED_MODEL}...")
        print("   (this takes ~1-2 mins for 30 conversations)\n")

        for i in range(0, total, BATCH_SIZE):
            batch = chunks[i : i + BATCH_SIZE]

            for chunk in batch:
                chunk_id = f"{chunk.source}__chunk_{chunk.chunk_idx}"

                # Skip if already in Chroma
                existing = self.collection.get(ids=[chunk_id])
                if existing["ids"]:
                    skipped += 1
                    continue

                vector = embed_text(chunk.text)
                if vector is None:
                    continue

                self.collection.add(
                    ids        = [chunk_id],
                    embeddings = [vector],
                    documents  = [chunk.text],
                    metadatas  = [chunk.metadata],
                )
                added += 1

            # Progress
            done = min(i + BATCH_SIZE, total)
            pct  = int(done / total * 100)
            bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
            print(f"   [{bar}] {pct}% ({done}/{total})", end="\r")
            time.sleep(0.1)

        print(f"\n\n✅ Added: {added} | Skipped (existing): {skipped}")
        print(f"📦 Total in collection: {self.collection.count()}\n")
        return added

    def query(
        self,
        query_text:   str,
        n_results:    int = 5,
        doc_type:     Optional[str] = None,
    ) -> list[dict]:
        """
        Semantic search against your corpus.
        Optionally filter by doc_type (prd / strategy / email / etc.)
        """
        vector = embed_text(query_text)
        if vector is None:
            return []

        where = {"doc_type": doc_type} if doc_type else None

        results = self.collection.query(
            query_embeddings = [vector],
            n_results        = n_results,
            where            = where,
            include          = ["documents", "metadatas", "distances"],
        )

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
                "similarity": round(1 - dist, 3),   # cosine: 1=identical
            })

        return output

    def stats(self) -> dict:
        count = self.collection.count()
        if count == 0:
            return {"total": 0}

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


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not check_ollama():
        exit(1)

    store = CorpusStore()
    stats = store.stats()
    print(f"📊 Current stats: {stats}\n")

    # Test query
    print("🔍 Test query: 'PRD structure and requirements'")
    results = store.query("PRD structure and requirements", n_results=3)
    for r in results:
        print(f"   [{r['similarity']:.3f}] {r['title']} ({r['doc_type']})")
        print(f"   {r['text'][:150]}...\n")
