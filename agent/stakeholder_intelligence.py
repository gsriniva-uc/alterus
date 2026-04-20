"""
stakeholder_intelligence.py
Builds and maintains stakeholder profiles from corpus data.

For each person in the user's network:
- Extracts communication style preferences
- Identifies topics they care about
- Detects tone that works vs triggers pushback
- Tracks relationship health over time
"""

import os
import json
import re
from pathlib import Path
from datetime import datetime

PROFILES_DIR = Path("data/stakeholder_profiles")
PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def get_all_profiles(user_email: str) -> list:
    """Return all stakeholder profiles for a user."""
    profiles = []
    for f in sorted(PROFILES_DIR.glob(f"{user_email.replace('@','_')}*.json")):
        try:
            profiles.append(json.loads(f.read_text()))
        except Exception:
            continue
    return profiles


def get_profile(user_email: str, stakeholder_name: str) -> dict:
    """Get a single stakeholder profile."""
    safe = _safe_name(user_email, stakeholder_name)
    path = PROFILES_DIR / f"{safe}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_profile(profile: dict):
    """Save a stakeholder profile to disk."""
    safe = _safe_name(
        profile.get("user_email", "default"),
        profile.get("name", "unknown")
    )
    path = PROFILES_DIR / f"{safe}.json"
    path.write_text(json.dumps(profile, indent=2))


def _safe_name(user_email: str, name: str) -> str:
    combined = f"{user_email}_{name}".lower()
    return re.sub(r"[^a-z0-9_]", "_", combined)[:60]


def analyze_stakeholders(user_email: str, known_stakeholders: list = None) -> list:
    """
    Analyze corpus to build stakeholder profiles.
    known_stakeholders: list of names to analyze (e.g. ["Jason Wong", "Raghu"])
    If None, extracts names from corpus automatically.
    """
    try:
        from retrieval.retriever import Retriever
        from agent.drafter import generate

        retriever = Retriever(user_id=user_email)

        # Use provided stakeholders or defaults
        if not known_stakeholders:
            known_stakeholders = _extract_stakeholder_names(retriever)

        profiles = []
        for name in known_stakeholders:
            profile = _analyze_single_stakeholder(
                name, user_email, retriever, generate
            )
            save_profile(profile)
            profiles.append(profile)

        return profiles

    except Exception as e:
        print(f"Stakeholder analysis error: {e}")
        return []


def _extract_stakeholder_names(retriever) -> list:
    """
    Auto-extract stakeholder names from any user's corpus.
    Uses NLP patterns to find proper nouns that appear frequently.
    Works for any user regardless of their network.
    """
    try:
        results = retriever.multi_search([
            "email meeting discussion",
            "reply response sent",
            "team stakeholder colleague",
        ], top_k=15)

        corpus_text = " ".join(r.get("text", "") for r in results)

        # Extract capitalized names using pattern matching
        # Matches "First Last" or "First" patterns
        import re
        from collections import Counter

        # Find all capitalized word sequences (likely names)
        name_pattern = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)\b")
        candidates = name_pattern.findall(corpus_text)

        # Filter out common non-name words
        skip_words = {
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday", "January", "February", "March",
            "April", "May", "June", "July", "August", "September",
            "October", "November", "December", "The", "This", "That",
            "From", "Subject", "Dear", "Best", "Thanks", "Regards",
            "Please", "Meeting", "Email", "Teams", "Slack", "Zoom",
            "Microsoft", "Google", "ServiceNow", "Anthropic", "Claude",
            "Customer", "Engine", "Feature", "Store", "Platform",
        }

        filtered = [
            n for n in candidates
            if not any(word in skip_words for word in n.split())
            and len(n) > 3
        ]

        # Return most frequent names
        counter = Counter(filtered)
        top_names = [name for name, count in counter.most_common(10) if count >= 2]

        return top_names[:8] if top_names else []

    except Exception as e:
        print(f"Name extraction error: {e}")
        return []


def _analyze_single_stakeholder(
    name: str,
    user_email: str,
    retriever,
    generate
) -> dict:
    """Analyze a single stakeholder from corpus data."""

    # Search corpus for interactions with this person
    try:
        results = retriever.multi_search([
            f"{name} email message",
            f"{name} meeting discussion",
            f"{name} reply response",
        ], top_k=5)
        corpus_context = "\n---\n".join(
            r.get("text", "")[:300] for r in results[:5]
        )
    except Exception:
        corpus_context = "No corpus data available yet."

    if not corpus_context.strip() or corpus_context == "No corpus data available yet.":
        # Return a starter profile
        return _starter_profile(name, user_email)

    # Ask Claude to analyze the stakeholder
    system_prompt = """You are an expert at analyzing professional communication patterns.
Analyze the provided communication samples and extract insights about a person's communication style.
Always respond in valid JSON only."""

    user_message = f"""Analyze these communication samples involving {name} and return a JSON profile:

{{
  "name": "{name}",
  "role": "inferred role or Unknown",
  "communication_style": "direct|collaborative|formal|casual|analytical",
  "preferred_tone": "one sentence describing ideal tone when communicating with them",
  "response_triggers": ["what gets positive responses"],
  "avoid": ["what causes friction or negative reactions"],
  "key_topics": ["topics they care about most"],
  "relationship_health": "strong|neutral|needs_attention",
  "relationship_signal": "one sentence explaining the relationship health",
  "draft_instruction": "specific instruction for the AI when drafting to this person",
  "last_updated": "{datetime.now().isoformat()}"
}}

Communication samples:
{corpus_context}

Return ONLY the JSON object."""

    try:
        raw = generate(system_prompt, user_message, temperature=0.3)
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            profile = json.loads(json_match.group())
        else:
            profile = json.loads(raw.strip())

        profile["user_email"] = user_email
        profile["name"] = name
        profile["source"] = "corpus_analysis"
        return profile

    except Exception as e:
        return _starter_profile(name, user_email)


def _starter_profile(name: str, user_email: str) -> dict:
    """Return a starter profile when no corpus data exists."""
    return {
        "name": name,
        "user_email": user_email,
        "role": "Unknown",
        "communication_style": "unknown",
        "preferred_tone": "Build corpus data to unlock personalized insights",
        "response_triggers": [],
        "avoid": [],
        "key_topics": [],
        "relationship_health": "neutral",
        "relationship_signal": "No interaction history yet",
        "draft_instruction": f"No specific style data for {name} yet",
        "source": "starter",
        "last_updated": datetime.now().isoformat(),
    }


def update_profile_from_draft(
    user_email: str,
    stakeholder_name: str,
    draft: str,
    feedback: str,
    context: str,
) -> bool:
    """Update stakeholder profile after an approved draft."""
    if not stakeholder_name or not draft:
        return False

    profile = get_profile(user_email, stakeholder_name)
    if not profile:
        return False

    # Track interaction history
    history = profile.get("interaction_history", [])
    history.append({
        "timestamp": datetime.now().isoformat(),
        "feedback": feedback,
        "draft_snippet": draft[:100],
    })
    profile["interaction_history"] = history[-10:]  # keep last 10

    # Update relationship health based on feedback
    if feedback == "thumbs_up":
        profile["relationship_health"] = "strong"
        profile["relationship_signal"] = "Recent interactions going well"
    elif feedback == "thumbs_down":
        profile["relationship_health"] = "needs_attention"
        profile["relationship_signal"] = "Recent draft needed improvement"

    profile["last_updated"] = datetime.now().isoformat()
    save_profile(profile)
    return True
