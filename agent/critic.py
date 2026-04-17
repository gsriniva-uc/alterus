"""
critic.py
Evaluates draft quality on multiple dimensions.
Scores 0-1 per dimension. Low scores trigger retry in the ReAct loop.
Also writes scores to LangSmith for the self-healing layer.
"""

import sys
import os
import re
import requests
from dataclasses import dataclass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class CritiqueResult:
    style_match:    float   # does it sound like Ganesh? (0-1)
    completeness:   float   # does it fully address the task? (0-1)
    tone_calibration: float # right tone for the audience? (0-1)
    conciseness:    float   # appropriately concise? (0-1)
    overall:        float   # weighted average
    passed:         bool    # True if overall >= threshold
    feedback:       str     # specific improvement notes
    retry_reason:   str     # what to tell the reasoner if retrying


PASS_THRESHOLD = 0.70   # overall score needed to pass to HITL


def evaluate_draft(
    draft:       str,
    task_type:   str,
    audience:    str,
    input_text:  str,
) -> CritiqueResult:
    """
    Heuristic evaluation of draft quality.
    Fast, no LLM call needed for basic checks.
    """
    feedback_notes = []
    draft_lower    = draft.lower()
    draft_len      = len(draft.split())

    # ── Style match ───────────────────────────────────────────────────────────
    style_score = 1.0
    # Penalize generic AI filler phrases
    filler_phrases = [
        "i hope this email finds you well",
        "i am writing to",
        "please do not hesitate",
        "as per our conversation",
        "i wanted to reach out",
        "touch base",
        "circle back",
        "going forward",
    ]
    filler_hits = sum(1 for p in filler_phrases if p in draft_lower)
    if filler_hits > 0:
        style_score -= (filler_hits * 0.15)
        feedback_notes.append(f"Remove {filler_hits} filler phrase(s): sounds generic not like Ganesh")

    # Check for action-oriented ending
    action_endings = ["next step", "let me know", "thoughts?", "can we", "please", "by "]
    has_action = any(p in draft_lower[-200:] for p in action_endings)
    if not has_action:
        style_score -= 0.1
        feedback_notes.append("Add a clear next step or ask at the end")

    style_score = max(0.0, min(1.0, style_score))

    # ── Completeness ─────────────────────────────────────────────────────────
    completeness_score = 1.0

    if task_type == "prd":
        required_sections = ["problem", "goal", "non-goal", "success metric"]
        missing = [s for s in required_sections if s not in draft_lower]
        if missing:
            completeness_score -= len(missing) * 0.15
            feedback_notes.append(f"PRD missing sections: {missing}")

    elif task_type in ("email", "reply"):
        if draft_len < 30:
            completeness_score -= 0.3
            feedback_notes.append("Response too short — add more context")
        if "error" in draft_lower[:20]:
            completeness_score = 0.0
            feedback_notes.append("Draft generation failed")

    completeness_score = max(0.0, min(1.0, completeness_score))

    # ── Tone calibration ─────────────────────────────────────────────────────
    tone_score = 1.0

    if audience == "vp":
        # VP emails should be concise
        if draft_len > 250:
            tone_score -= 0.2
            feedback_notes.append("Too long for VP audience — trim to under 200 words")
        # Should not be overly casual
        casual_markers = ["hey,", "hey!", "lol", "btw", "gonna"]
        if any(m in draft_lower for m in casual_markers):
            tone_score -= 0.2
            feedback_notes.append("Too casual for VP audience")

    elif audience == "engineer":
        # Engineer messages can be more technical and direct
        if draft_len > 400:
            tone_score -= 0.1
            feedback_notes.append("Slightly long for engineer — consider trimming")

    tone_score = max(0.0, min(1.0, tone_score))

    # ── Conciseness ───────────────────────────────────────────────────────────
    conciseness_score = 1.0

    if task_type == "teams" and draft_len > 80:
        conciseness_score -= 0.3
        feedback_notes.append("Teams message too long — keep under 4 sentences")
    elif task_type == "email" and draft_len > 300:
        conciseness_score -= 0.2
        feedback_notes.append("Email is verbose — aim for under 200 words")

    conciseness_score = max(0.0, min(1.0, conciseness_score))

    # ── Overall weighted score ────────────────────────────────────────────────
    overall = (
        style_score         * 0.30 +
        completeness_score  * 0.35 +
        tone_score          * 0.20 +
        conciseness_score   * 0.15
    )

    passed      = overall >= PASS_THRESHOLD
    feedback    = " | ".join(feedback_notes) if feedback_notes else "Draft looks good"
    retry_reason = feedback if not passed else ""

    return CritiqueResult(
        style_match       = round(style_score, 3),
        completeness      = round(completeness_score, 3),
        tone_calibration  = round(tone_score, 3),
        conciseness       = round(conciseness_score, 3),
        overall           = round(overall, 3),
        passed            = passed,
        feedback          = feedback,
        retry_reason      = retry_reason,
    )


def format_critique_for_langsmith(critique: CritiqueResult) -> dict:
    """Format critique scores for LangSmith feedback logging."""
    return {
        "style_match":       critique.style_match,
        "completeness":      critique.completeness,
        "tone_calibration":  critique.tone_calibration,
        "conciseness":       critique.conciseness,
        "overall":           critique.overall,
        "passed":            critique.passed,
        "feedback":          critique.feedback,
    }


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_draft = """
Hi Jason,

I hope this email finds you well. I am writing to provide you with an update 
on the Customer Engine project. We have made significant progress and I wanted 
to touch base with you about the current status.

Best regards,
Ganesh
    """.strip()

    result = evaluate_draft(
        draft      = test_draft,
        task_type  = "email",
        audience   = "vp",
        input_text = "Status update on Customer Engine for Jason Wong",
    )

    print(f"Style match:       {result.style_match}")
    print(f"Completeness:      {result.completeness}")
    print(f"Tone calibration:  {result.tone_calibration}")
    print(f"Conciseness:       {result.conciseness}")
    print(f"Overall:           {result.overall}")
    print(f"Passed:            {result.passed}")
    print(f"Feedback:          {result.feedback}")
