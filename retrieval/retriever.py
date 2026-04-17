"""
retriever.py
Searches the Chroma corpus for relevant context given a query.
Supports multi-query retrieval and optional doc_type filtering.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest.embedder import CorpusStore

# ── Config ────────────────────────────────────────────────────────────────────
CHROMA_DIR    = Path("data/chroma_db")
DEFAULT_TOP_K = 5


class Retriever:
    def __init__(self):
        self.store = CorpusStore(CHROMA_DIR)

    def search(
        self,
        query:    str,
        top_k:    int  = DEFAULT_TOP_K,
        doc_type: str  = None,
    ) -> list[dict]:
        """Single query search."""
        return self.store.query(query, n_results=top_k, doc_type=doc_type)

    def multi_search(
        self,
        queries:  list[str],
        top_k:    int = 3,
        doc_type: str = None,
    ) -> list[dict]:
        """
        Run multiple queries and deduplicate results.
        Used by the ReAct reasoner to pull context from multiple angles.
        """
        seen    = set()
        results = []

        for query in queries:
            hits = self.store.query(query, n_results=top_k, doc_type=doc_type)
            for hit in hits:
                key = hit["source"] + str(hit.get("chunk_idx", ""))
                if key not in seen:
                    seen.add(key)
                    results.append(hit)

        # Sort by similarity descending
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k * 2]

    def get_style_examples(self, task_type: str, top_k: int = 3) -> list[dict]:
        """
        Pull writing style examples relevant to the task type.
        Used by persona.py to build the style system prompt.
        """
        query_map = {
            "email":        "professional email tone VP stakeholder communication",
            "teams":        "Teams message quick response direct tone",
            "prd":          "PRD product requirements goals success metrics",
            "requirements": "requirements document user stories acceptance criteria",
            "presentation": "presentation slide deck executive summary",
            "strategy":     "strategy document initiative roadmap",
        }
        query = query_map.get(task_type, "professional writing communication")
        return self.search(query, top_k=top_k)

    def format_context(self, results: list[dict], max_chars: int = 3000) -> str:
        """Format retrieved chunks into a clean context string for the prompt."""
        if not results:
            return "No relevant context found."

        parts = []
        total = 0
        for r in results:
            snippet = (
                f"[From: {r['title']} | Type: {r['doc_type']} | "
                f"Relevance: {r['similarity']:.2f}]\n{r['text']}"
            )
            if total + len(snippet) > max_chars:
                break
            parts.append(snippet)
            total += len(snippet)

        return "\n\n---\n\n".join(parts)


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    r = Retriever()

    print("── Test 1: Single search ─────────────────────")
    results = r.search("PRD requirements product goals", top_k=3)
    for hit in results:
        print(f"  [{hit['similarity']:.3f}] {hit['title']} ({hit['doc_type']})")

    print("\n── Test 2: Multi search ──────────────────────")
    results = r.multi_search([
        "VP email communication style",
        "executive stakeholder update",
        "leadership message tone",
    ])
    for hit in results:
        print(f"  [{hit['similarity']:.3f}] {hit['title']} ({hit['doc_type']})")

    print("\n── Test 3: Style examples for email ──────────")
    examples = r.get_style_examples("email")
    print(r.format_context(examples))
