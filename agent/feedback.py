"""
feedback.py
Handles human feedback collection and LangSmith logging.
Powers the self-healing layer.

Two parts:
  1. log_feedback()     — called when user thumbs up/down a draft
  2. batch_healer()     — reads feedback from LangSmith, finds patterns,
                          proposes prompt improvements
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── LangSmith client ──────────────────────────────────────────────────────────
try:
    from langsmith import Client
    ls_client = Client() if os.getenv("LANGSMITH_API_KEY") else None
except Exception:
    ls_client = None

# ── Local feedback log (fallback if LangSmith unavailable) ───────────────────
FEEDBACK_LOG = Path("data/feedback_log.json")


# ── Feedback types ────────────────────────────────────────────────────────────

FEEDBACK_SCORES = {
    "thumbs_up":    1.0,
    "thumbs_down":  0.0,
    "edited":       0.5,   # user edited before approving
    "rejected":     0.0,
    "approved":     1.0,
}


# ── Core feedback logger ──────────────────────────────────────────────────────

def log_feedback(
    run_id:       Optional[str],
    feedback_type: str,          # thumbs_up / thumbs_down / edited / approved
    draft:        str,
    input_text:   str,
    task_type:    str,
    edited_draft: Optional[str] = None,
    comment:      Optional[str] = None,
) -> dict:
    """
    Log human feedback to LangSmith + local file.
    Called every time user clicks thumbs up/down, approves, or edits.
    """
    score    = FEEDBACK_SCORES.get(feedback_type, 0.5)
    was_edited = edited_draft and edited_draft.strip() != draft.strip()

    # Compute edit distance if user edited
    edit_distance = 0
    if was_edited and edited_draft:
        orig_words = set(draft.lower().split())
        new_words  = set(edited_draft.lower().split())
        edit_distance = len(orig_words.symmetric_difference(new_words))

    feedback_entry = {
        "timestamp":     datetime.now().isoformat(),
        "run_id":        run_id or "no_run_id",
        "feedback_type": feedback_type,
        "score":         score,
        "task_type":     task_type,
        "input_preview": input_text[:100],
        "draft_preview": draft[:100],
        "was_edited":    was_edited,
        "edit_distance": edit_distance,
        "comment":       comment or "",
    }

    # ── Log to LangSmith ──────────────────────────────────────────────────────
    if ls_client and run_id:
        try:
            ls_client.create_feedback(
                run_id   = run_id,
                key      = "human_feedback",
                score    = score,
                comment  = comment or f"{feedback_type} | task={task_type} | edited={was_edited}",
            )

            if was_edited and edit_distance > 0:
                ls_client.create_feedback(
                    run_id  = run_id,
                    key     = "edit_distance",
                    score   = max(0, 1 - (edit_distance / 50)),
                    comment = f"User edited {edit_distance} words",
                )
        except Exception as e:
            print(f"   ⚠️  LangSmith feedback error: {e}")

    # ── Log locally ───────────────────────────────────────────────────────────
    _append_local_feedback(feedback_entry)

    return feedback_entry


def _append_local_feedback(entry: dict):
    """Append feedback to local JSON log."""
    FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = []
    if FEEDBACK_LOG.exists():
        try:
            log = json.loads(FEEDBACK_LOG.read_text())
        except Exception:
            log = []
    log.append(entry)
    FEEDBACK_LOG.write_text(json.dumps(log, indent=2))


def load_local_feedback() -> list[dict]:
    """Load all local feedback entries."""
    if not FEEDBACK_LOG.exists():
        return []
    try:
        return json.loads(FEEDBACK_LOG.read_text())
    except Exception:
        return []


# ── Batch healer ──────────────────────────────────────────────────────────────

def run_batch_healer(days_back: int = 7) -> dict:
    """
    Analyze recent feedback to find patterns and propose improvements.

    Reads:
      - Local feedback log
      - LangSmith runs (if available)

    Returns:
      - patterns found
      - proposed prompt improvements
      - stats summary
    """
    from agent.drafter import generate

    print("\n🔄 Running batch healer...")
    feedback = load_local_feedback()

    # Filter to recent feedback
    cutoff = datetime.now() - timedelta(days=days_back)
    recent = [
        f for f in feedback
        if datetime.fromisoformat(f["timestamp"]) > cutoff
    ]

    if not recent:
        return {"status": "no_feedback", "message": "No feedback in last 7 days"}

    # ── Compute stats ─────────────────────────────────────────────────────────
    total         = len(recent)
    thumbs_up     = sum(1 for f in recent if f["feedback_type"] == "thumbs_up")
    thumbs_down   = sum(1 for f in recent if f["feedback_type"] == "thumbs_down")
    edited        = sum(1 for f in recent if f.get("was_edited"))
    avg_score     = sum(f["score"] for f in recent) / total if total else 0

    # By task type
    by_type = {}
    for f in recent:
        t = f.get("task_type","unknown")
        by_type.setdefault(t, {"count":0,"score_sum":0,"edits":0})
        by_type[t]["count"]     += 1
        by_type[t]["score_sum"] += f["score"]
        by_type[t]["edits"]     += 1 if f.get("was_edited") else 0

    # Find worst performing task types
    worst_types = sorted(
        [(t, d["score_sum"]/d["count"], d["edits"])
         for t, d in by_type.items()],
        key=lambda x: x[1]
    )[:3]

    stats = {
        "total_feedback":  total,
        "thumbs_up":       thumbs_up,
        "thumbs_down":     thumbs_down,
        "edits":           edited,
        "avg_score":       round(avg_score, 3),
        "approval_rate":   f"{int(avg_score*100)}%",
        "by_task_type":    {t: {"avg_score": round(d["score_sum"]/d["count"],2),
                                "count": d["count"],
                                "edit_rate": f"{int(d['edits']/d['count']*100)}%"}
                            for t, d in by_type.items()},
        "worst_performers": worst_types,
    }

    print(f"   📊 {total} feedback entries | {thumbs_up}👍 {thumbs_down}👎 {edited}✏️")
    print(f"   📊 Approval rate: {stats['approval_rate']}")

    # ── Ask LLM to analyze patterns ───────────────────────────────────────────
    print("   🧠 Analyzing patterns with LLM...")

    feedback_summary = json.dumps({
        "stats":         stats,
        "worst_types":   worst_types,
        "recent_negative": [
            {"task": f["task_type"], "input": f["input_preview"],
             "draft": f["draft_preview"], "type": f["feedback_type"]}
            for f in recent if f["score"] < 0.5
        ][:10]
    }, indent=2)

    analysis = generate(
        system_prompt = """You analyze AI agent feedback to identify improvement patterns.
Be specific and actionable. Return only JSON.""",
        user_message  = f"""Analyze this feedback data from a Ganesh Srinivasan AI clone agent:

{feedback_summary}

Identify:
1. What types of drafts are getting thumbs down most?
2. What writing patterns is the user editing away?
3. What specific prompt improvements would help?

Return JSON:
{{
  "top_issues": ["issue 1", "issue 2", "issue 3"],
  "prompt_fixes": [
    {{"area": "email tone", "current_problem": "...", "suggested_fix": "..."}},
    {{"area": "VP communication", "current_problem": "...", "suggested_fix": "..."}}
  ],
  "overall_assessment": "one sentence"
}}""",
        temperature = 0.2,
    )

    # Parse analysis
    try:
        import re
        clean = re.sub(r"```json|```", "", analysis).strip()
        start = clean.find("{")
        end   = clean.rfind("}") + 1
        parsed = json.loads(clean[start:end]) if start >= 0 else {}
    except Exception:
        parsed = {"raw": analysis}

    result = {
        "status":    "complete",
        "stats":     stats,
        "analysis":  parsed,
        "run_at":    datetime.now().isoformat(),
    }

    # Save healer report
    report_path = Path("data/healer_report.json")
    report_path.write_text(json.dumps(result, indent=2))
    print(f"   ✅ Healer report saved to {report_path}")

    return result


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║   Self-Healing Batch Analyzer            ║")
    print("╚══════════════════════════════════════════╝\n")

    # Add some test feedback
    log_feedback(
        run_id        = "test-run-001",
        feedback_type = "thumbs_down",
        draft         = "I hope this email finds you well. I wanted to touch base...",
        input_text    = "Email to Jason about Customer Engine",
        task_type     = "email",
        comment       = "Too generic, not direct enough",
    )
    log_feedback(
        run_id        = "test-run-002",
        feedback_type = "thumbs_up",
        draft         = "Jason, Customer Engine fix deployed. PR approved by Sibanjan.",
        input_text    = "Quick update to Jason",
        task_type     = "email",
    )

    # Run healer
    result = run_batch_healer()
    print("\n── Stats ─────────────────────────────────────")
    print(json.dumps(result["stats"], indent=2))
    print("\n── Analysis ──────────────────────────────────")
    print(json.dumps(result.get("analysis",{}), indent=2))
