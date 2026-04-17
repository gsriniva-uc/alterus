"""
ingest.py
Orchestrates the full ingest pipeline:
  1. Chunk all scraped conversation files
  2. Embed with Ollama nomic-embed-text
  3. Store in local Chroma vector DB

Run:
    python ingest/ingest.py
"""

import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest.chunker  import Chunker
from ingest.embedder import CorpusStore, check_ollama

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR   = Path("data/claude_conversations")
CHROMA_DIR = Path("data/chroma_db")


def run_ingest():
    print("╔══════════════════════════════════════════╗")
    print("║   Ganesh Agent — Corpus Ingest Pipeline  ║")
    print("╚══════════════════════════════════════════╝\n")

    # ── Step 1: Check Ollama ──────────────────────────────────────────────────
    print("── Step 1: Checking Ollama ───────────────────")
    if not check_ollama():
        print("\n💡 Fix:")
        print("   ollama serve          # start Ollama")
        print("   ollama pull nomic-embed-text  # pull model")
        sys.exit(1)

    # ── Step 2: Check data directory ─────────────────────────────────────────
    print("── Step 2: Checking data directory ──────────")
    if not DATA_DIR.exists():
        print(f"❌ Data directory not found: {DATA_DIR}")
        print("   Run the scraper first: python ingest/scrape_claude.py")
        sys.exit(1)

    txt_files = list(DATA_DIR.glob("*.txt"))
    if not txt_files:
        print(f"❌ No .txt files found in {DATA_DIR}")
        print("   Run the scraper first: python ingest/scrape_claude.py")
        sys.exit(1)

    print(f"✅ Found {len(txt_files)} conversation files\n")

    # ── Step 3: Chunk ─────────────────────────────────────────────────────────
    print("── Step 3: Chunking conversations ───────────")
    chunker = Chunker()
    chunks  = chunker.chunk_directory(DATA_DIR)

    # Show doc type breakdown
    type_counts = {}
    for c in chunks:
        type_counts[c.doc_type] = type_counts.get(c.doc_type, 0) + 1
    print("   Doc types detected:")
    for dt, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"     {dt:<15} {count} chunks")
    print()

    # ── Step 4: Embed + Store ─────────────────────────────────────────────────
    print("── Step 4: Embedding + storing in Chroma ────")
    store = CorpusStore(CHROMA_DIR)
    store.add_chunks(chunks)

    # ── Step 5: Verify ────────────────────────────────────────────────────────
    print("── Step 5: Verification ─────────────────────")
    stats = store.stats()
    print(f"   Total chunks stored:    {stats['total_chunks']}")
    print(f"   Unique documents:       {stats['unique_documents']}")
    print(f"   Chunks by doc type:")
    for dt, count in stats.get("chunks_by_doc_type", {}).items():
        print(f"     {dt:<15} {count}")
    print()

    # ── Step 6: Test retrieval ────────────────────────────────────────────────
    print("── Step 6: Test retrieval ────────────────────")
    test_queries = [
        "PRD structure and requirements",
        "VP level email communication style",
        "LangGraph agent architecture",
    ]

    for q in test_queries:
        print(f"\n   Query: '{q}'")
        results = store.query(q, n_results=2)
        for r in results:
            print(f"   [{r['similarity']:.3f}] {r['title']} ({r['doc_type']})")

    print("\n╔══════════════════════════════════════════╗")
    print("║   ✅ Ingest complete! Corpus is ready.   ║")
    print("║   Next: build the retrieval + agent.     ║")
    print("╚══════════════════════════════════════════╝\n")


if __name__ == "__main__":
    run_ingest()
