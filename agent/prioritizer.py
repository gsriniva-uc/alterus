"""
prioritizer.py
Copilot-inspired prioritization engine for emails and calendar events.

Scoring signals (same as Microsoft Copilot for Outlook):
  1. Relationship strength  — corpus-based interaction frequency
  2. Direct mention         — your name in the email
  3. Action required        — email asks you to do something
  4. Response expected      — sender waiting for reply
  5. VIP sender             — known key stakeholders
  6. Time sensitivity       — deadlines, urgent keywords
  7. Thread activity        — active back-and-forth
  8. Unread status          — unread weighted higher
  9. Meeting priority       — attendee importance + topic relevance
  10. Content signals       — decision, approve, blocker keywords
"""

import re
import json
from pathlib import Path
from datetime import datetime, date
from typing import Optional

# ── VIP Senders (your key stakeholders) ──────────────────────────────────────
VIP_SENDERS = {
    # Tier 1 — direct chain (highest weight)
    "jason wong":    10,
    "jason.wong":    10,
    "luke":           9,
    "luke.h":         9,

    # Tier 2 — key stakeholders
    "raghu":          8,
    "raghunathan":    8,
    "sibanjan":       7,
    "sibanjan.das":   7,
    "jerry jiang":    7,
    "jerry.jiang":    7,
    "senthil":        7,
    "senthil.v":      7,
    "shariq":         6,
    "ashraf":         6,

    # Tier 3 — extended team
    "raja":           5,
    "ragunath":       5,
    "tony":           5,
    "ramya":          5,
    "rohit":          5,
    "jimmy":          5,
}

# ── Action keywords ───────────────────────────────────────────────────────────
ACTION_KEYWORDS = [
    "please", "can you", "could you", "would you",
    "need you to", "asking you", "request",
    "action required", "action needed", "your input",
    "approve", "approval needed", "sign off",
    "review", "feedback", "thoughts on",
    "decision needed", "decide", "your call",
]

URGENCY_KEYWORDS = [
    "urgent", "asap", "immediately", "right away",
    "today", "eod", "end of day", "by close",
    "critical", "blocker", "blocking", "stopped",
    "emergency", "escalation", "escalating",
]

RESPONSE_EXPECTED = [
    "let me know", "please respond", "awaiting your",
    "waiting for you", "can you confirm", "please confirm",
    "what do you think", "your thoughts", "thoughts?",
    "?",  # question mark = response expected
]

TIME_SENSITIVE = [
    "today", "tomorrow", "this week", "by friday",
    "by monday", "by eod", "by cob", "deadline",
    "due", "expires", "expiring", "time-sensitive",
]

NOISE_SENDERS = [
    "noreply", "no-reply", "donotreply", "postmaster",
    "notifications", "automated", "mailer-daemon",
    "newsletter", "analytics", "reporting",
    "servicenow-news", "dt-notifications", "training",
    "org-wide", "all-hands", "companywide",
    "zoom", "sharepoint", "github", "jira", "confluence",
]

NOISE_SUBJECTS = [
    "accepted:", "declined:", "cancelled:", "updated:",
    "invitation:", "has invited you", "meeting invitation",
    "automatic reply", "out of office", "auto-reply",
    "unsubscribe", "newsletter", "promotional",
    "password reset", "verification", "otp", "code:",
    "order confirmation", "receipt", "invoice",
    "training required", "mandatory training",
    "org-wide announcement", "all employee",
    "recording available", "transcript available",
]


# ── Email Scorer ──────────────────────────────────────────────────────────────

def score_email(email: dict, my_name: str = "Ganesh") -> dict:
    """
    Score an email on 0-100 scale using Copilot-inspired signals.
    Returns the email dict with added 'priority_score' and 'priority_signals'.
    """
    score   = 0
    signals = []
    sender  = (email.get("from","") or "").lower()
    subject = (email.get("subject","") or "").lower()
    body    = (email.get("body","") or email.get("preview","") or "").lower()
    combined = f"{sender} {subject} {body}"

    # ── Signal 1: Noise filter (hard skip) ───────────────────────────────────
    if any(n in sender for n in NOISE_SENDERS):
        return {**email, "priority_score": -100,
                "priority_label": "🗑️ Noise",
                "priority_signals": ["automated sender"]}

    if any(n in subject for n in NOISE_SUBJECTS):
        return {**email, "priority_score": -100,
                "priority_label": "🗑️ Noise",
                "priority_signals": ["automated subject"]}

    # ── Signal 2: VIP sender ──────────────────────────────────────────────────
    vip_score = 0
    for vip, weight in VIP_SENDERS.items():
        if vip in sender:
            vip_score = weight * 5
            signals.append(f"VIP sender: {vip} (+{vip_score})")
            break
    score += vip_score

    # ── Signal 3: Direct @mention ─────────────────────────────────────────────
    if my_name.lower() in body or my_name.lower() in subject:
        score += 15
        signals.append("@mentioned you (+15)")

    # ── Signal 4: Action required ─────────────────────────────────────────────
    action_hits = sum(1 for kw in ACTION_KEYWORDS if kw in combined)
    if action_hits > 0:
        action_boost = min(action_hits * 5, 20)
        score += action_boost
        signals.append(f"action required (+{action_boost})")

    # ── Signal 5: Response expected ───────────────────────────────────────────
    response_hits = sum(1 for kw in RESPONSE_EXPECTED if kw in combined)
    if response_hits > 0:
        score += min(response_hits * 4, 15)
        signals.append(f"response expected (+{min(response_hits*4,15)})")

    # ── Signal 6: Urgency ─────────────────────────────────────────────────────
    urgency_hits = sum(1 for kw in URGENCY_KEYWORDS if kw in combined)
    if urgency_hits > 0:
        urgency_boost = min(urgency_hits * 8, 25)
        score += urgency_boost
        signals.append(f"urgent (+{urgency_boost})")

    # ── Signal 7: Time sensitivity ────────────────────────────────────────────
    time_hits = sum(1 for kw in TIME_SENSITIVE if kw in combined)
    if time_hits > 0:
        score += min(time_hits * 5, 15)
        signals.append(f"time-sensitive (+{min(time_hits*5,15)})")

    # ── Signal 8: Unread ──────────────────────────────────────────────────────
    if email.get("unread", False):
        score += 10
        signals.append("unread (+10)")

    # ── Signal 9: Recency ─────────────────────────────────────────────────────
    received = email.get("receivedAt","") or email.get("time","")
    if received:
        try:
            if "AM" in received or "PM" in received:
                # Already formatted as time — it's today
                score += 8
                signals.append("received today (+8)")
            elif "Yesterday" in received:
                score += 4
                signals.append("received yesterday (+4)")
        except Exception:
            pass

    # ── Signal 10: Business-critical keywords ────────────────────────────────
    biz_keywords = [
        "customer engine", "feature store", "nba", "csx",
        "q2", "q1", "roadmap", "okr", "sprint",
        "halliburton", "plep", "stardog", "ztsd",
        "decision", "blocker", "risk", "escalat",
    ]
    biz_hits = sum(1 for kw in biz_keywords if kw in combined)
    if biz_hits > 0:
        biz_boost = min(biz_hits * 4, 16)
        score += biz_boost
        signals.append(f"business-critical topic (+{biz_boost})")

    # ── Determine label ───────────────────────────────────────────────────────
    if score >= 60:
        label = "🔴 High"
    elif score >= 35:
        label = "🟡 Medium"
    elif score >= 10:
        label = "🟢 Normal"
    else:
        label = "⚪ Low"

    return {
        **email,
        "priority_score":   score,
        "priority_label":   label,
        "priority_signals": signals,
    }


def prioritize_emails(emails: list[dict], top_n: int = 10,
                       include_all: bool = False) -> list[dict]:
    """
    Score and rank emails. Returns top_n prioritized emails.
    Filters out noise by default.
    """
    scored = [score_email(e) for e in emails]

    # Remove noise
    if not include_all:
        scored = [e for e in scored if e["priority_score"] >= 0]

    # Sort by score descending
    scored.sort(key=lambda x: x["priority_score"], reverse=True)

    return scored[:top_n]


# ── Calendar Scorer ───────────────────────────────────────────────────────────

def score_calendar_event(event: dict) -> dict:
    """
    Score a calendar event for priority display.
    Signals: attendee importance, topic relevance, time proximity.
    """
    score    = 0
    signals  = []
    title    = (event.get("title","") or "").lower()
    attendees = event.get("attendees",[])
    if isinstance(attendees, str):
        attendees = [attendees]
    attendees_str = " ".join(attendees).lower()
    body     = (event.get("body","") or "").lower()
    combined = f"{title} {attendees_str} {body}"
    start    = event.get("start","") or ""

    # ── Filter: not today ────────────────────────────────────────────────────
    today = date.today().isoformat()
    is_today = False
    try:
        clean = start.replace("Z","").replace(" ","T")
        if "+" in clean[10:]: clean = clean[:clean.rindex("+")]
        event_date = datetime.fromisoformat(clean).date().isoformat()
        if event_date == today:
            is_today = True
        else:
            return {**event, "priority_score": -999,
                    "priority_label": "📅 Not today",
                    "priority_signals": [f"date: {event_date}"]}
    except Exception:
        # Can't parse date — keep it
        is_today = True

    # ── Skip noise events ────────────────────────────────────────────────────
    skip_titles = ["test", "cancelled", "canceled", "declined",
                   "free", "busy", "block", "focus time",
                   "lunch", "break", "personal"]
    if any(s in title for s in skip_titles):
        return {**event, "priority_score": -100,
                "priority_label": "⚪ Skip",
                "priority_signals": ["noise event"]}

    # ── Signal 1: VIP attendee ────────────────────────────────────────────────
    for vip, weight in VIP_SENDERS.items():
        if vip in attendees_str:
            vip_boost = weight * 4
            score += vip_boost
            signals.append(f"VIP attendee: {vip} (+{vip_boost})")
            break

    # ── Signal 2: Important topic ─────────────────────────────────────────────
    important_topics = [
        "customer engine", "feature store", "nba", "roadmap",
        "q2", "sprint", "planning", "review", "sync",
        "halliburton", "plep", "stardog", "strategy",
        "eai", "ccx", "edp", "apex",
    ]
    topic_hits = sum(1 for t in important_topics if t in combined)
    if topic_hits > 0:
        topic_boost = min(topic_hits * 6, 20)
        score += topic_boost
        signals.append(f"important topic (+{topic_boost})")

    # ── Signal 3: Meeting time proximity ─────────────────────────────────────
    try:
        clean = start.replace("Z","").replace(" ","T")
        if "+" in clean[10:]: clean = clean[:clean.rindex("+")]
        meeting_time = datetime.fromisoformat(clean)
        now          = datetime.now()
        mins_until   = (meeting_time - now).total_seconds() / 60

        if 0 <= mins_until <= 30:
            score += 30
            signals.append(f"starting in {int(mins_until)}min (+30)")
        elif 0 <= mins_until <= 60:
            score += 20
            signals.append(f"starting in {int(mins_until)}min (+20)")
        elif 0 <= mins_until <= 120:
            score += 10
            signals.append(f"starting in {int(mins_until/60*10)/10}h (+10)")
        elif mins_until < 0:
            score -= 10  # already past
            signals.append("already started (-10)")
    except Exception:
        pass

    # ── Signal 4: External attendees ─────────────────────────────────────────
    if any(ext in attendees_str for ext in
           ["halliburton","external","@gmail","@outlook"]):
        score += 10
        signals.append("external attendee (+10)")

    # ── Signal 5: Has meeting description ────────────────────────────────────
    if body and len(body) > 50:
        score += 5
        signals.append("has agenda (+5)")

    # ── Label ────────────────────────────────────────────────────────────────
    if score >= 40:
        label = "🔴 High priority"
    elif score >= 20:
        label = "🟡 Important"
    else:
        label = "🟢 Normal"

    return {
        **event,
        "priority_score":   score,
        "priority_label":   label,
        "priority_signals": signals,
    }


def prioritize_calendar(events: list[dict], top_n: int = 5) -> list[dict]:
    """Score and rank today's calendar events."""
    scored = [score_calendar_event(e) for e in events]
    # Remove non-today and noise
    scored = [e for e in scored if e["priority_score"] >= 0]
    scored.sort(key=lambda x: x["priority_score"], reverse=True)
    return scored[:top_n]


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_emails = [
        {"from":"jason.wong@servicenow.com","subject":"Customer Engine Q2 priorities",
         "body":"Ganesh can you send the status update today? Need this for the board.",
         "unread":True,"time":"10:32 AM"},
        {"from":"newsletter@servicenow.com","subject":"ServiceNow News Weekly",
         "body":"This week in ServiceNow...","unread":True,"time":"9:00 AM"},
        {"from":"raghu@servicenow.com","subject":"ZTSD ML decision needed",
         "body":"We need your decision on the Stardog vs Neo4j by EOD.",
         "unread":True,"time":"11:15 AM"},
        {"from":"noreply@zoom.us","subject":"Recording available",
         "body":"Your Zoom recording is ready.","unread":False,"time":"8:00 AM"},
        {"from":"sibanjan.das@servicenow.com","subject":"Re: Semantic router fix",
         "body":"Fix is ready for review. Can you approve the PR?",
         "unread":True,"time":"10:45 AM"},
        {"from":"training@servicenow.com","subject":"Mandatory training due",
         "body":"Please complete your training.","unread":True,"time":"7:00 AM"},
    ]

    print("── Email Priority Scores ─────────────────────────────")
    ranked = prioritize_emails(test_emails, top_n=10, include_all=True)
    for e in ranked:
        if e["priority_score"] >= 0:
            print(f"\n  {e['priority_label']} ({e['priority_score']:3d}) "
                  f"{e['from'][:30]}")
            print(f"  Subject: {e['subject'][:50]}")
            print(f"  Signals: {', '.join(e['priority_signals'][:3])}")
