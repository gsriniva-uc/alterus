"""
persona.py
Builds the user's system prompt dynamically.
Reads user identity from config.py (environment variables).
No hardcoded names — works for any user.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.retriever import Retriever

def build_system_prompt(task_type: str = "email", user_config: dict = None) -> str:
    """
    Build a system prompt for the given task type.
    Uses user_config dict if provided (from extension API calls),
    otherwise falls back to config.py environment variables.
    """

    # Get user identity
    if user_config:
        name         = user_config.get("name", "")
        title        = user_config.get("title", "")
        company      = user_config.get("company", "")
        stakeholders = user_config.get("stakeholders", [])
        tone         = user_config.get("tone", "balanced")
    else:
        try:
            from config import (USER_NAME, USER_TITLE, USER_COMPANY,
                               USER_STAKEHOLDERS, USER_TONE)
            name         = USER_NAME
            title        = USER_TITLE
            company      = USER_COMPANY
            stakeholders = USER_STAKEHOLDERS
            tone         = USER_TONE
        except Exception:
            name         = "the user"
            title        = ""
            company      = ""
            stakeholders = []
            tone         = "balanced"

    # Try to get style examples from corpus
    style_examples = ""
    try:
        retriever = Retriever()
        results   = retriever.search(f"{task_type} writing style examples", top_k=2)
        if results:
            style_examples = "\n\nSTYLE EXAMPLES FROM YOUR PAST WRITING:\n"
            for r in results[:2]:
                style_examples += f"---\n{r['text'][:300]}\n"
    except Exception:
        pass

    # Tone instruction
    tone_map = {
        "direct":     "Be direct, concise, and confident. No filler phrases. Get to the point immediately.",
        "balanced":   "Be professional and clear. Direct but not blunt. Warm but not informal.",
        "diplomatic": "Be thoughtful, warm, and considerate. Soften where needed. Build relationship.",
    }
    tone_instruction = tone_map.get(tone, tone_map["balanced"])

    # Task-specific instructions
    task_map = {
        "email":    "Write email replies that are clear, professional, and action-oriented.",
        "teams":    "Write concise Teams/chat replies. Shorter than email. Conversational but professional.",
        "prd":      "Write structured product documents with clear requirements and acceptance criteria.",
        "strategy": "Write strategic documents that are data-driven and executive-ready.",
        "meeting":  "Write meeting prep briefs that are focused and actionable.",
    }
    task_instruction = task_map.get(task_type, task_map["email"])

    # Build stakeholder context
    stakeholder_context = ""
    if stakeholders:
        names = stakeholders if isinstance(stakeholders, list) else stakeholders.split(",")
        stakeholder_context = f"\nKEY STAKEHOLDERS: {', '.join(n.strip() for n in names)}"

    prompt = f"""You are acting as {name or 'the user'}{f' — {title}' if title else ''}{f' at {company}' if company else ''}.

IDENTITY:
- You write all communications in {name or "the user"}'s voice
- Every draft should sound like it came directly from them
- Match their professional tone and style exactly{stakeholder_context}

TONE: {tone_instruction}

TASK: {task_instruction}

RULES:
- Never add filler phrases like "I hope this email finds you well"
- Never sign off with "Best regards" unless it matches the user's style
- Never invent facts, projects, or commitments not in the source material
- Keep drafts concise — under 150 words unless more detail is clearly needed
- Write in first person as {name or 'the user'}
{style_examples}"""

    return prompt
