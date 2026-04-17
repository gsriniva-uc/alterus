"""
classifier.py
Classifies incoming messages/tasks + runs LLM-based sentiment analysis.
Uses keyword heuristics for classification (fast, no LLM).
Uses Ollama llama3.2 for sentiment (rich, nuanced).
"""

import re
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SentimentResult:
    sentiment:    str    # positive / neutral / tense / frustrated / urgent / appreciative
    urgency:      str    # high / medium / low
    tone:         str    # one sentence description
    emoji:        str    # visual indicator for UI
    color:        str    # hex color for UI badge
    action_needed: bool  # does this require a response?
    raw:          dict = field(default_factory=dict)


@dataclass
class Classification:
    task_type:   str           # email / teams / prd / requirements / strategy / reply
    source:      str           # outlook / teams / manual
    urgency:     str           # high / medium / low
    audience:    str           # vp / director / peer / engineer / external
    confidence:  float         # 0-1
    reasoning:   str           # why this classification
    sentiment:   Optional[SentimentResult] = None  # LLM-based sentiment


# ── Keyword heuristics ────────────────────────────────────────────────────────

TASK_KEYWORDS = {
    "prd":          ["prd", "product requirement", "write a prd", "create a prd",
                     "requirements doc", "product spec", "feature spec"],
    "requirements": ["requirements", "user stories", "acceptance criteria",
                     "functional requirements", "technical requirements"],
    "strategy":     ["strategy", "roadmap", "strategic", "initiative", "proposal",
                     "business case", "executive summary"],
    "email":        ["email", "subject:", "draft an email", "write an email",
                     "send to", "outlook"],
    "teams":        ["teams message", "teams chat", "dm", "slack message",
                     "reply in teams", "respond to teams"],
}

AUDIENCE_KEYWORDS = {
    "vp":       ["vp", "vice president", "jason wong", "executive", "leadership"],
    "director": ["director", "senior director", "head of", "raghu"],
    "engineer": ["engineer", "developer", "tech", "sibanjan", "jerry", "senthil"],
    "external": ["customer", "client", "partner", "halliburton", "external"],
    "peer":     ["pm", "product manager", "team", "colleague"],
}

URGENCY_KEYWORDS = {
    "high":   ["urgent", "asap", "today", "immediately", "blocker", "critical", "eod"],
    "medium": ["this week", "soon", "follow up", "reminder"],
    "low":    ["when you get a chance", "fyi", "no rush", "low priority"],
}

# Sentiment → UI mapping
SENTIMENT_UI = {
    "positive":     {"emoji": "😊", "color": "#22c55e"},
    "appreciative": {"emoji": "🙏", "color": "#22c55e"},
    "neutral":      {"emoji": "😐", "color": "#8b8fa8"},
    "tense":        {"emoji": "😤", "color": "#f59e0b"},
    "frustrated":   {"emoji": "😠", "color": "#ef4444"},
    "urgent":       {"emoji": "🚨", "color": "#ef4444"},
    "curious":      {"emoji": "🤔", "color": "#6366f1"},
    "pressured":    {"emoji": "😬", "color": "#f59e0b"},
}


# ── LLM sentiment analysis ────────────────────────────────────────────────────

def analyze_sentiment(text: str, sender: str = "") -> SentimentResult:
    """
    Use llama3.2 to analyze sentiment, urgency, and tone of a message.
    Returns structured SentimentResult for UI display.
    """
    from agent.drafter import generate

    prompt = """You analyze the sentiment and tone of professional messages.
Return ONLY a valid JSON object. No explanation, no markdown, no preamble."""

    user_msg = f"""Analyze this message from {sender if sender else 'a colleague'}:

\"\"\"{text[:800]}\"\"\"

Return ONLY this JSON (no other text):
{{
  "sentiment": "one of: positive, neutral, tense, frustrated, urgent, appreciative, curious, pressured",
  "urgency": "one of: high, medium, low",
  "tone": "one sentence describing the emotional tone",
  "action_needed": true or false,
  "key_signal": "the specific word or phrase that most reveals the tone"
}}"""

    raw_response = generate(
        system_prompt = prompt,
        user_message  = user_msg,
        temperature   = 0.1,
    )

    # Parse JSON from response
    try:
        clean = raw_response.strip()
        if clean.startswith("```"):
            lines = clean.split("\n")
            clean = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        start = clean.find("{")
        end   = clean.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(clean[start:end])
        else:
            raise ValueError("No JSON found")

        sentiment = data.get("sentiment", "neutral")
        ui        = SENTIMENT_UI.get(sentiment, SENTIMENT_UI["neutral"])

        return SentimentResult(
            sentiment     = sentiment,
            urgency       = data.get("urgency", "medium"),
            tone          = data.get("tone", ""),
            emoji         = ui["emoji"],
            color         = ui["color"],
            action_needed = data.get("action_needed", True),
            raw           = data,
        )

    except Exception as e:
        # Graceful fallback — don't break the UI
        return SentimentResult(
            sentiment     = "neutral",
            urgency       = "medium",
            tone          = "Could not analyze tone",
            emoji         = "😐",
            color         = "#8b8fa8",
            action_needed = True,
            raw           = {},
        )


# ── Batch sentiment for corpus ────────────────────────────────────────────────

def batch_analyze_messages(messages: list[dict]) -> list[dict]:
    """
    Run sentiment analysis on a list of messages.
    Each message dict needs 'body'/'message' and 'from' keys.
    Adds 'sentiment' key to each message dict.
    """
    results = []
    for msg in messages:
        text   = msg.get("body") or msg.get("message", "")
        sender = msg.get("from", "")
        s      = analyze_sentiment(text, sender)
        results.append({
            **msg,
            "sentiment": {
                "sentiment":     s.sentiment,
                "urgency":       s.urgency,
                "tone":          s.tone,
                "emoji":         s.emoji,
                "color":         s.color,
                "action_needed": s.action_needed,
            }
        })
    return results


# ── Main classifier ───────────────────────────────────────────────────────────

def classify(
    text:            str,
    source:          str  = "manual",
    sender:          str  = "",
    subject:         str  = "",
    run_sentiment:   bool = True,
) -> Classification:
    """
    Classify a message/task into a structured Classification.
    Optionally runs LLM sentiment analysis.
    """
    combined = (text + " " + subject + " " + sender).lower()

    # ── Task type ─────────────────────────────────────────────────────────────
    task_scores  = {task: sum(1 for kw in kws if kw in combined)
                    for task, kws in TASK_KEYWORDS.items()}
    default_task = "reply" if source in ("outlook", "teams") else "email"
    best_task    = max(task_scores, key=task_scores.get)
    task_type    = best_task if task_scores[best_task] > 0 else default_task
    if source == "teams" and task_scores.get(task_type, 0) == 0:
        task_type = "teams"

    # ── Audience ──────────────────────────────────────────────────────────────
    aud_scores = {a: sum(1 for kw in kws if kw in combined)
                  for a, kws in AUDIENCE_KEYWORDS.items()}
    best_aud   = max(aud_scores, key=aud_scores.get)
    audience   = best_aud if aud_scores[best_aud] > 0 else "peer"

    # ── Urgency ───────────────────────────────────────────────────────────────
    urg_scores = {u: sum(1 for kw in kws if kw in combined)
                  for u, kws in URGENCY_KEYWORDS.items()}
    best_urg   = max(urg_scores, key=urg_scores.get)
    urgency    = best_urg if urg_scores[best_urg] > 0 else "medium"

    # ── Confidence ────────────────────────────────────────────────────────────
    max_score  = max(task_scores.values()) if task_scores else 0
    confidence = min(0.5 + (max_score * 0.15), 0.95)

    reasoning = (
        f"Source: {source} | "
        f"Task signals: {dict((k,v) for k,v in task_scores.items() if v > 0)} | "
        f"Audience: {audience} | Urgency: {urgency}"
    )

    # ── Sentiment (LLM) ───────────────────────────────────────────────────────
    sentiment = None
    if run_sentiment and text.strip():
        try:
            sentiment = analyze_sentiment(text, sender)
            # Override urgency if sentiment says high
            if sentiment.urgency == "high" and urgency == "medium":
                urgency = "high"
        except Exception:
            pass

    return Classification(
        task_type  = task_type,
        source     = source,
        urgency    = urgency,
        audience   = audience,
        confidence = confidence,
        reasoning  = reasoning,
        sentiment  = sentiment,
    )


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_cases = [
        ("Ganesh, can you send over the current state of Customer Engine and Q2 targets? Also need to understand the dependency on Sibanjan's team. Thanks, Jason",
         "outlook", "Jason Wong"),
        ("3 sprint items past due. Can we sync today? Also need Stardog vs Neo4j decision.",
         "teams", "Senthil V."),
        ("Great work on the Q1 delivery. Let's lock Q2 roadmap before all-hands Friday.",
         "teams", "Jason Wong"),
        ("Write a PRD for the Feature Store capability",
         "manual", ""),
    ]

    for text, source, sender in test_cases:
        print(f"\nFrom: {sender or 'manual'} | Source: {source}")
        print(f"Text: {text[:80]}...")
        c = classify(text, source=source, sender=sender, run_sentiment=True)
        print(f"Task: {c.task_type} | Audience: {c.audience} | Urgency: {c.urgency}")
        if c.sentiment:
            s = c.sentiment
            print(f"Sentiment: {s.emoji} {s.sentiment} | Tone: {s.tone}")
            print(f"Action needed: {s.action_needed} | Key signal: {s.raw.get('key_signal','')}")
        print()
