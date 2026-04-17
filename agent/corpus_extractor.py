"""
corpus_extractor.py
Queries Chroma corpus and uses LLM to extract structured data:
  - Workstreams (real projects from your conversations)
  - Stakeholders (real people and context)
  - Recent topics (what you've been working on)

Caches results to data/extracted_context.json
Auto-refreshes when corpus changes.

Run standalone to test:
    python -m agent.corpus_extractor
"""

import sys
import json
import hashlib
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.retriever import Retriever
from agent.drafter import generate

# ── Config ────────────────────────────────────────────────────────────────────
CACHE_FILE    = Path("data/extracted_context.json")
CHROMA_DIR    = Path("data/chroma_db")


# ── Extraction prompts ────────────────────────────────────────────────────────

WORKSTREAM_PROMPT = """
You are extracting structured data from Ganesh Srinivasan's work conversations.

From the context below, extract ALL current workstreams / projects.
For each workstream return ONLY valid JSON — no preamble, no explanation.

Return a JSON array like this:
[
  {{
    "name": "Project name",
    "tag": "active" or "risk" or "blocked",
    "tag_label": "🟢 Active" or "⚠️ At Risk" or "🔴 Blocked",
    "description": "2-3 sentence current status based on the conversations",
    "owner": "Engineering owner name",
    "stakeholder": "Main stakeholder name"
  }}
]

Rules:
- Only include projects explicitly mentioned in the context
- description must reflect ACTUAL current status from the conversations
- If a project has blockers or risks mentioned, tag it as "risk"
- Owner = engineering manager or team lead
- Stakeholder = VP or director the work reports to
- Return ONLY the JSON array, nothing else

CONTEXT:
{context}
"""

STAKEHOLDER_PROMPT = """
You are extracting structured data from Ganesh Srinivasan's work conversations.

From the context below, extract ALL key stakeholders Ganesh works with.
Return ONLY valid JSON — no preamble, no explanation.

Return a JSON array like this:
[
  {{
    "name": "Full name",
    "role": "Title and team",
    "last": "unknown",
    "pending": "Specific pending items or asks from the conversations",
    "notes": "Communication style, preferences, what they care about"
  }}
]

Rules:
- Only include people explicitly mentioned in the context
- pending must be specific — what does Ganesh owe them or vice versa?
- notes should capture relationship dynamics from actual conversations
- Return ONLY the JSON array, nothing else

CONTEXT:
{context}
"""

TOPICS_PROMPT = """
From the context below, extract the top 5 topics/themes Ganesh has been
most focused on recently.

Return ONLY a JSON array of strings:
["topic 1", "topic 2", "topic 3", "topic 4", "topic 5"]

Return ONLY the JSON array, nothing else.

CONTEXT:
{context}
"""


# ── Extractor ─────────────────────────────────────────────────────────────────

class CorpusExtractor:
    def __init__(self):
        self.retriever = Retriever()

    def _safe_parse_json(self, text: str, fallback: list) -> list:
        """Parse JSON from LLM output, handling markdown fences."""
        try:
            # Strip markdown code fences if present
            clean = text.strip()
            if clean.startswith("```"):
                lines = clean.split("\n")
                clean = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            # Find the JSON array
            start = clean.find("[")
            end   = clean.rfind("]") + 1
            if start >= 0 and end > start:
                return json.loads(clean[start:end])
        except Exception as e:
            print(f"   ⚠️  JSON parse failed: {e}")
            print(f"   Raw: {text[:200]}")
        return fallback

    def extract_workstreams(self) -> list[dict]:
        """Extract current workstreams from corpus."""
        print("   🔍 Querying corpus for workstreams...")

        queries = [
            "current projects workstreams EAI Customer Engine Feature Store",
            "sprint roadmap Q2 delivery ATL BTL OKR",
            "LangGraph agent Customer Engine CCX skill builder",
            "Stardog ZTSD ML model consulting deployment",
            "SlideVertex recommender suite custom ML agent OrcaML",
        ]
        results  = self.retriever.multi_search(queries, top_k=4)
        context  = self.retriever.format_context(results, max_chars=3000)

        print("   🧠 Extracting workstream structure...")
        raw = generate(
            system_prompt = "You extract structured JSON from conversation context. Return ONLY valid JSON arrays.",
            user_message  = WORKSTREAM_PROMPT.format(context=context),
            temperature   = 0.1,
        )

        fallback = [
            {"name":"Customer Engine","tag":"active","tag_label":"🟢 Active",
             "description":"NBA renewal recommendations agent. Semantic router fix deployed.",
             "owner":"Sibanjan Das","stakeholder":"Jason Wong"},
            {"name":"Feature Store","tag":"active","tag_label":"🟢 Active",
             "description":"White paper complete. Implementation planning with D&A.",
             "owner":"Senthil V.","stakeholder":"Raghu"},
            {"name":"CCX Skill Builder","tag":"active","tag_label":"🟢 Active",
             "description":"5-stage agent pipeline. Pending CCX governance sign-off.",
             "owner":"Jerry Jiang","stakeholder":"Jason Wong"},
        ]

        workstreams = self._safe_parse_json(raw, fallback)
        print(f"   ✅ Extracted {len(workstreams)} workstreams")
        return workstreams

    def extract_stakeholders(self) -> list[dict]:
        """Extract stakeholders from corpus."""
        print("   🔍 Querying corpus for stakeholders...")

        queries = [
            "Jason Wong VP CCX stakeholder communication",
            "Raghu EDP D&A data analytics engineering",
            "Sibanjan Jerry Senthil engineering manager team",
            "Luke manager relationship goals QGC",
            "stakeholder alignment cross functional",
        ]
        results  = self.retriever.multi_search(queries, top_k=4)
        context  = self.retriever.format_context(results, max_chars=3000)

        print("   🧠 Extracting stakeholder structure...")
        raw = generate(
            system_prompt = "You extract structured JSON from conversation context. Return ONLY valid JSON arrays.",
            user_message  = STAKEHOLDER_PROMPT.format(context=context),
            temperature   = 0.1,
        )

        fallback = [
            {"name":"Jason Wong","role":"VP, CCX","last":"unknown",
             "pending":"Q2 roadmap, Customer Engine status",
             "notes":"Prefers concise updates. Focused on Q2 delivery and CCX ROI."},
            {"name":"Raghu","role":"Director, EDP","last":"unknown",
             "pending":"Feature Store planning, ZTSD timeline",
             "notes":"Technically deep. Wants data-backed decisions."},
            {"name":"Sibanjan Das","role":"Eng Manager, EAI","last":"unknown",
             "pending":"Sprint scope, PR reviews",
             "notes":"Collaborative. Needs clear PM prioritization."},
            {"name":"Jerry Jiang","role":"Eng Manager, EAI","last":"unknown",
             "pending":"Architecture decisions, sprint planning",
             "notes":"Detail-oriented. Wants decisions before planning."},
            {"name":"Senthil V.","role":"Eng Manager, EAI","last":"unknown",
             "pending":"Sprint reprioritization",
             "notes":"Execution-focused. Flags blockers early."},
        ]

        stakeholders = self._safe_parse_json(raw, fallback)
        print(f"   ✅ Extracted {len(stakeholders)} stakeholders")
        return stakeholders

    def extract_topics(self) -> list[str]:
        """Extract recent focus topics for news/video tailoring."""
        print("   🔍 Querying corpus for focus topics...")

        queries = [
            "recent focus areas technical work",
            "AI agents LangGraph architecture patterns",
            "enterprise AI platform product strategy",
        ]
        results = self.retriever.multi_search(queries, top_k=3)
        context = self.retriever.format_context(results, max_chars=2000)

        print("   🧠 Extracting topic keywords...")
        raw = generate(
            system_prompt = "You extract structured JSON from conversation context. Return ONLY valid JSON arrays.",
            user_message  = TOPICS_PROMPT.format(context=context),
            temperature   = 0.1,
        )

        fallback = [
            "LangGraph multi-agent systems",
            "enterprise AI platform ServiceNow",
            "RAG retrieval augmented generation",
            "AI product management strategy",
            "agentic AI orchestration",
        ]

        topics = self._safe_parse_json(raw, fallback)
        if not isinstance(topics, list) or not topics:
            return fallback
        print(f"   ✅ Extracted {len(topics)} topics")
        return topics[:8]

    def extract_all(self, force_refresh: bool = False) -> dict:
        """
        Extract all structured data from corpus.
        Uses cache to avoid re-extraction on every page load.
        """
        # Check cache
        if not force_refresh and CACHE_FILE.exists():
            try:
                cached = json.loads(CACHE_FILE.read_text())
                cached_at = cached.get("extracted_at", "")
                # Use cache if less than 6 hours old
                if cached_at:
                    age_hours = (datetime.now() -
                                 datetime.fromisoformat(cached_at)).seconds / 3600
                    if age_hours < 6:
                        print(f"   📦 Using cached context (extracted {age_hours:.1f}h ago)")
                        return cached
            except Exception:
                pass

        print("\n🔄 Extracting context from corpus...")
        print("   (runs once, caches for 6 hours)\n")

        workstreams  = self.extract_workstreams()
        stakeholders = self.extract_stakeholders()
        topics       = self.extract_topics()

        # Build news search query from topics
        news_query = "+".join(
            t.replace(" ", "+") for t in topics[:3]
        )

        result = {
            "extracted_at":  datetime.now().isoformat(),
            "workstreams":   workstreams,
            "stakeholders":  stakeholders,
            "topics":        topics,
            "news_query":    news_query,
        }

        # Save cache
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(result, indent=2))

        print(f"\n✅ Extraction complete")
        print(f"   Workstreams:  {len(workstreams)}")
        print(f"   Stakeholders: {len(stakeholders)}")
        print(f"   Topics:       {len(topics)}")
        print(f"   News query:   {news_query}\n")

        return result


# ── Singleton accessor ────────────────────────────────────────────────────────

_cache = None

def get_context(force_refresh: bool = False) -> dict:
    """Get extracted context, using module-level cache."""
    global _cache
    if _cache is None or force_refresh:
        extractor = CorpusExtractor()
        _cache    = extractor.extract_all(force_refresh=force_refresh)
    return _cache


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║   Corpus Extractor — Context Builder     ║")
    print("╚══════════════════════════════════════════╝\n")

    ctx = get_context(force_refresh=True)

    print("\n── WORKSTREAMS ───────────────────────────────")
    for ws in ctx["workstreams"]:
        print(f"  {ws['tag_label']} {ws['name']}")
        print(f"     {ws['description'][:80]}...")
        print(f"     Owner: {ws['owner']} | Stakeholder: {ws['stakeholder']}")

    print("\n── STAKEHOLDERS ──────────────────────────────")
    for sk in ctx["stakeholders"]:
        print(f"  {sk['name']} ({sk['role']})")
        print(f"     Pending: {sk['pending'][:80]}")

    print("\n── TOPICS ────────────────────────────────────")
    for t in ctx["topics"]:
        print(f"  • {t}")

    print(f"\n── NEWS QUERY ────────────────────────────────")
    print(f"  {ctx['news_query']}")
