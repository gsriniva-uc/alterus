"""
self_healer.py
Analyzes feedback patterns and proposes persona prompt improvements.

How it works:
1. Reads all feedback from data/feedback_log.json
2. Loads recent draft history from corpus
3. Asks Claude to identify patterns and propose fixes
4. Saves report to data/healer_report.json
5. Optionally auto-applies fixes to persona.py
"""

import os
import json
from pathlib import Path
from datetime import datetime


FEEDBACK_FILE = Path("data/feedback_log.json")
HEALER_REPORT = Path("data/healer_report.json")
PERSONA_FILE  = Path("agent/persona.py")


def load_feedback() -> list:
    if not FEEDBACK_FILE.exists():
        return []
    try:
        return json.loads(FEEDBACK_FILE.read_text())
    except Exception:
        return []


def save_feedback(entry: dict):
    """Append a feedback entry to the log."""
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    feedback = load_feedback()
    feedback.append(entry)
    FEEDBACK_FILE.write_text(json.dumps(feedback, indent=2))


def run_batch_healer(auto_apply: bool = False) -> dict:
    """
    Analyze all feedback and propose prompt improvements.
    Returns a report dict with issues, fixes, and overall assessment.
    """
    feedback = load_feedback()

    if len(feedback) < 2:
        return {
            "error": "Not enough feedback yet. Need at least 2 entries.",
            "feedback_count": len(feedback),
            "run_at": datetime.now().isoformat(),
        }

    # ── Summarize feedback patterns ───────────────────────────────────────────
    total        = len(feedback)
    thumbs_up    = sum(1 for f in feedback if f.get("feedback_type") == "thumbs_up")
    thumbs_down  = sum(1 for f in feedback if f.get("feedback_type") == "thumbs_down")
    edited       = sum(1 for f in feedback if f.get("feedback_type") == "edited")
    approval_rate = int((thumbs_up / total) * 100) if total else 0

    # Collect edited drafts for pattern analysis
    edit_examples = []
    for f in feedback:
        if f.get("feedback_type") == "edited" and f.get("draft") and f.get("edited_draft"):
            original = f["draft"][:200]
            edited_v = f["edited_draft"][:200]
            if original != edited_v:
                edit_examples.append({
                    "platform":  f.get("platform", "email"),
                    "task_type": f.get("task_type", ""),
                    "original":  original,
                    "edited":    edited_v,
                })

    # Collect thumbs down examples
    negative_examples = []
    for f in feedback:
        if f.get("feedback_type") == "thumbs_down":
            negative_examples.append({
                "platform":  f.get("platform", "email"),
                "task_type": f.get("task_type", ""),
                "draft":     f.get("draft", "")[:200],
                "context":   f.get("input_text", "")[:100],
            })

    # ── Ask Claude to analyze patterns ────────────────────────────────────────
    try:
        from agent.drafter import generate

        system_prompt = """You are an AI prompt engineer analyzing feedback on AI-generated drafts.
Your job is to identify patterns in user edits and thumbs-down feedback, then propose specific improvements.
Always respond in valid JSON only."""

        feedback_summary = f"""
FEEDBACK SUMMARY:
- Total feedback entries: {total}
- Thumbs up: {thumbs_up} ({approval_rate}% approval rate)
- Thumbs down: {thumbs_down}
- Edited by user: {edited}

EDIT EXAMPLES (what user changed):
{json.dumps(edit_examples[:5], indent=2)}

THUMBS DOWN EXAMPLES:
{json.dumps(negative_examples[:3], indent=2)}
"""

        user_message = f"""{feedback_summary}

Analyze these feedback patterns and return a JSON object with:
{{
  "top_issues": ["issue1", "issue2", "issue3"],
  "prompt_fixes": [
    {{
      "area": "Email length",
      "current_problem": "what's going wrong",
      "suggested_fix": "specific instruction to add to persona prompt",
      "priority": "high|medium|low"
    }}
  ],
  "overall_assessment": "2-3 sentence summary of draft quality and key improvements needed",
  "approval_rate": {approval_rate},
  "recommended_tone_adjustment": "none|more_direct|more_diplomatic|shorter|longer"
}}

Return ONLY the JSON object."""

        raw = generate(system_prompt, user_message, temperature=0.3)

        import re
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            analysis = json.loads(json_match.group())
        else:
            analysis = json.loads(raw.strip())

    except Exception as e:
        analysis = {
            "top_issues": ["Could not analyze — Claude API error"],
            "prompt_fixes": [],
            "overall_assessment": str(e),
            "approval_rate": approval_rate,
            "recommended_tone_adjustment": "none",
        }

    # ── Build report ──────────────────────────────────────────────────────────
    report = {
        "run_at":          datetime.now().isoformat(),
        "feedback_count":  total,
        "approval_rate":   approval_rate,
        "thumbs_up":       thumbs_up,
        "thumbs_down":     thumbs_down,
        "edited":          edited,
        "analysis":        analysis,
    }

    # Save report
    HEALER_REPORT.parent.mkdir(parents=True, exist_ok=True)
    HEALER_REPORT.write_text(json.dumps(report, indent=2))
    print(f"🔄 Self-healer report saved: {approval_rate}% approval rate")

    # ── Auto-apply fixes if requested ─────────────────────────────────────────
    if auto_apply and analysis.get("prompt_fixes"):
        try:
            _auto_apply_fixes(analysis["prompt_fixes"])
            report["auto_applied"] = True
        except Exception as e:
            report["auto_apply_error"] = str(e)

    return report


def _auto_apply_fixes(fixes: list):
    """
    Auto-apply high-priority fixes to persona.py.
    Only applies safe, additive changes.
    """
    if not PERSONA_FILE.exists():
        return

    persona_content = PERSONA_FILE.read_text()
    applied = []

    for fix in fixes:
        if fix.get("priority") != "high":
            continue

        instruction = fix.get("suggested_fix", "")
        if not instruction or len(instruction) < 10:
            continue

        # Only add if not already present
        if instruction[:30].lower() not in persona_content.lower():
            # Find the system prompt return and inject the fix
            if "return system_prompt" in persona_content:
                old = "return system_prompt"
                new = f"    # Auto-applied by self-healer {datetime.now().strftime('%Y-%m-%d')}\n    # Fix: {instruction}\n    return system_prompt"
                persona_content = persona_content.replace(old, new, 1)
                applied.append(fix["area"])

    if applied:
        PERSONA_FILE.write_text(persona_content)
        print(f"✅ Auto-applied fixes: {', '.join(applied)}")
