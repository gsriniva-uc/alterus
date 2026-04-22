"""
risk_engine.py
Communication Risk Engine using semantic similarity.

For each draft:
1. Embed the draft using Voyage AI
2. Compare vs stakeholder's successful/failed communication vectors
3. Score risk 0-100
4. Use Claude to explain why and suggest a fix
"""

import os
import json
import re
from pathlib import Path
from datetime import datetime

RISK_CACHE_DIR = Path("data/risk_profiles")
RISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)

MIN_INTERACTIONS = 5  # minimum needed to give risk score


def cosine_similarity(v1: list, v2: list) -> float:
    """Compute cosine similarity between two vectors."""
    if not v1 or not v2:
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    mag1 = sum(a * a for a in v1) ** 0.5
    mag2 = sum(b * b for b in v2) ** 0.5
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot / (mag1 * mag2)


def mean_vector(vectors: list) -> list:
    """Compute mean of a list of vectors."""
    if not vectors:
        return []
    n = len(vectors)
    dim = len(vectors[0])
    return [sum(v[i] for v in vectors) / n for i in range(dim)]


def embed_text(text: str) -> list:
    """Embed text using Voyage AI."""
    try:
        import voyageai
        client = voyageai.Client(api_key=os.getenv("VOYAGE_API_KEY", ""))
        result = client.embed([text[:2000]], model="voyage-3")
        return result.embeddings[0]
    except Exception as e:
        print(f"Embedding error: {e}")
        return []


def get_stakeholder_vectors(user_email: str, stakeholder_name: str) -> dict:
    """
    Build success and failure vector clusters for a stakeholder
    from the feedback log.
    """
    cache_path = RISK_CACHE_DIR / f"{user_email}_{stakeholder_name}.json".replace("/", "_").replace("@", "_")

    # Load feedback log
    feedback_path = Path("data/feedback_log.json")
    if not feedback_path.exists():
        return {"success": [], "failure": [], "count": 0}

    try:
        feedback = json.loads(feedback_path.read_text())
    except Exception:
        return {"success": [], "failure": [], "count": 0}

    # Filter by stakeholder
    relevant = [
        f for f in feedback
        if f.get("user_email") == user_email
        and stakeholder_name.lower() in f.get("input_text", "").lower()
        and f.get("draft")
    ]

    if len(relevant) < MIN_INTERACTIONS:
        return {"success": [], "failure": [], "count": len(relevant)}

    # Separate success and failure
    success_drafts = [
        f["draft"] for f in relevant
        if f.get("feedback_type") in ("thumbs_up", "approved")
    ]
    failure_drafts = [
        f["draft"] for f in relevant
        if f.get("feedback_type") in ("thumbs_down", "edited")
    ]

    # Embed all drafts
    success_vectors = [embed_text(d) for d in success_drafts if d]
    failure_vectors = [embed_text(d) for d in failure_drafts if d]

    # Filter out empty embeddings
    success_vectors = [v for v in success_vectors if v]
    failure_vectors = [v for v in failure_vectors if v]

    return {
        "success": success_vectors,
        "failure": failure_vectors,
        "count": len(relevant),
        "success_count": len(success_vectors),
        "failure_count": len(failure_vectors),
    }


def analyze_risk(
    draft: str,
    stakeholder_name: str,
    user_email: str,
    platform: str = "email",
) -> dict:
    """
    Analyze communication risk for a draft.

    Returns:
        risk_score: 0-100 (higher = more risky)
        explanation: why it's risky
        suggestion: specific fix
        confidence: how confident we are (based on data size)
        status: "scored" | "insufficient_data" | "error"
    """

    if not draft or not stakeholder_name:
        return {"status": "error", "error": "Missing draft or stakeholder"}

    # Get stakeholder vectors
    vectors = get_stakeholder_vectors(user_email, stakeholder_name)

    if vectors["count"] < MIN_INTERACTIONS:
        return {
            "status": "insufficient_data",
            "interactions_needed": MIN_INTERACTIONS - vectors["count"],
            "message": f"Need {MIN_INTERACTIONS - vectors['count']} more interactions with {stakeholder_name} to unlock risk analysis",
            "risk_score": None,
        }

    # Embed the current draft
    draft_vector = embed_text(draft)
    if not draft_vector:
        return {"status": "error", "error": "Could not embed draft"}

    # Compute similarity to success and failure clusters
    success_vectors = vectors.get("success", [])
    failure_vectors = vectors.get("failure", [])

    success_sim = 0.0
    failure_sim = 0.0

    if success_vectors:
        success_centroid = mean_vector(success_vectors)
        success_sim = cosine_similarity(draft_vector, success_centroid)

    if failure_vectors:
        failure_centroid = mean_vector(failure_vectors)
        failure_sim = cosine_similarity(draft_vector, failure_centroid)

    # Calculate risk score (0-100)
    if success_sim + failure_sim > 0:
        raw_risk = failure_sim / (success_sim + failure_sim)
    else:
        raw_risk = 0.5  # neutral if no signal

    risk_score = int(raw_risk * 100)

    # Determine risk level
    if risk_score >= 65:
        risk_level = "high"
        risk_emoji = "🔴"
    elif risk_score >= 40:
        risk_level = "medium"
        risk_emoji = "🟡"
    else:
        risk_level = "low"
        risk_emoji = "🟢"

    # Confidence based on data size
    total = vectors["count"]
    confidence = min(100, int((total / 20) * 100))

    # Ask Claude to explain and suggest fix
    explanation = ""
    suggestion = ""

    try:
        from agent.drafter import generate

        system_prompt = """You are an expert communication analyst.
Analyze why a draft might receive a negative response from a specific stakeholder.
Be specific and actionable. Under 60 words total for explanation + suggestion combined."""

        user_message = f"""Draft to {stakeholder_name}:
"{draft[:400]}"

Risk score: {risk_score}/100 ({risk_level})
Success similarity: {success_sim:.2f}
Failure similarity: {failure_sim:.2f}
Platform: {platform}

In 2 sentences max:
1. Why this draft is risky for {stakeholder_name} specifically
2. One specific fix (start with an action verb)

Format as JSON: {{"explanation": "...", "suggestion": "..."}}"""

        raw = generate(system_prompt, user_message, temperature=0.3)
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
            explanation = parsed.get("explanation", "")
            suggestion = parsed.get("suggestion", "")
    except Exception as e:
        explanation = f"Risk score: {risk_score}/100"
        suggestion = "Review tone and framing before sending"

    return {
        "status": "scored",
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_emoji": risk_emoji,
        "explanation": explanation,
        "suggestion": suggestion,
        "confidence": confidence,
        "interactions_analyzed": total,
        "success_similarity": round(success_sim, 3),
        "failure_similarity": round(failure_sim, 3),
        "stakeholder": stakeholder_name,
        "timestamp": datetime.now().isoformat(),
    }


def get_stakeholder_risk_summary(user_email: str, stakeholder_name: str) -> dict:
    """
    Get overall communication risk summary for a stakeholder.
    Used in the People tab dashboard.
    """
    feedback_path = Path("data/feedback_log.json")
    if not feedback_path.exists():
        return {"avg_risk": None, "best_tone": "unknown", "avoid": []}

    try:
        feedback = json.loads(feedback_path.read_text())
    except Exception:
        return {"avg_risk": None, "best_tone": "unknown", "avoid": []}

    relevant = [
        f for f in feedback
        if f.get("user_email") == user_email
        and stakeholder_name.lower() in f.get("input_text", "").lower()
    ]

    if len(relevant) < MIN_INTERACTIONS:
        return {
            "avg_risk": None,
            "message": f"Need {MIN_INTERACTIONS} interactions to unlock",
            "count": len(relevant)
        }

    approved = [f for f in relevant if f.get("feedback_type") == "thumbs_up"]
    rejected = [f for f in relevant if f.get("feedback_type") == "thumbs_down"]

    approval_rate = len(approved) / len(relevant) if relevant else 0
    avg_risk = int((1 - approval_rate) * 100)

    # Find best performing tone
    tone_counts = {}
    for f in approved:
        tone = f.get("tone", "balanced")
        tone_counts[tone] = tone_counts.get(tone, 0) + 1

    best_tone = max(tone_counts, key=tone_counts.get) if tone_counts else "balanced"

    return {
        "avg_risk": avg_risk,
        "approval_rate": int(approval_rate * 100),
        "best_tone": best_tone,
        "total_interactions": len(relevant),
        "approved": len(approved),
        "rejected": len(rejected),
    }
