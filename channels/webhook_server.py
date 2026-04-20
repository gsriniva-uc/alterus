"""
webhook_server.py
FastAPI server receiving Power Automate webhooks for:
  - Outlook email
  - Teams messages  
  - Calendar events

Fixes in v2:
  - Calendar deduplication (title+start combo)
  - Teams: filters out own messages
  - Email: strips HTML properly
  - Calendar: clears duplicates on startup

Run:
    python -m channels.webhook_server
"""

import sys
import json
import re
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR      = Path("data")
INBOX_FILE    = DATA_DIR / "live_emails.json"
TEAMS_FILE    = DATA_DIR / "live_teams.json"
CALENDAR_FILE = DATA_DIR / "live_calendar.json"
PORT          = 8000

# Your Zoom/Teams display name — messages from yourself are filtered out
MY_NAME       = "Ganesh Srinivasan"
MY_EMAIL      = "ganesh.srinivasan@servicenow.com"

app = FastAPI(title="Alterus Webhook Server v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Storage helpers ───────────────────────────────────────────────────────────

def load_json(path: Path) -> list:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def save_json(path: Path, data: list):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def prepend_item(path: Path, item: dict, max_items: int = 50):
    """Add item to front of list, deduplicate by id."""
    items = load_json(path)
    items = [x for x in items if x.get("id") != item.get("id")]
    items.insert(0, item)
    save_json(path, items[:max_items])


def dedup_calendar(events: list) -> list:
    """
    Remove duplicate calendar events.
    Two events are duplicates if they share the same title AND
    start time (first 16 chars = YYYY-MM-DDTHH:MM).
    Keep the most recent version.
    """
    seen = {}
    for event in events:
        key = f"{event.get('title','')}|{event.get('start','')[:16]}"
        if key not in seen:
            seen[key] = event
    return list(seen.values())


# ── Startup: clean existing calendar duplicates ───────────────────────────────

@app.on_event("startup")
async def startup_cleanup():
    """Clean up any duplicate calendar events from before the fix."""
    existing = load_json(CALENDAR_FILE)
    if existing:
        cleaned = dedup_calendar(existing)
        if len(cleaned) < len(existing):
            save_json(CALENDAR_FILE, cleaned)
            print(f"🧹 Cleaned {len(existing) - len(cleaned)} duplicate calendar events")
    print(f"🚀 Webhook server ready on port {PORT}")


# ── Webhook endpoints ─────────────────────────────────────────────────────────

@app.post("/webhook/email")
async def receive_email(request: Request):
    """Receive new Outlook email from Power Automate."""
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "detail": "invalid JSON"}

    # Skip emails sent BY Ganesh (Power Automate may trigger on sent items)
    sender_raw   = data.get("from", "")
    sender_email = _extract_email_address(sender_raw)
    sender_name  = _extract_email_name(sender_raw)
    if MY_EMAIL.lower() in sender_email.lower() or MY_NAME.lower() in sender_name.lower():
        print(f"📧 Skipping own email: {data.get('subject','')[:40]}")
        return {"status": "skipped", "reason": "own email"}

    email = {
        "id":             data.get("id", f"email_{datetime.now().timestamp()}"),
        "from":           sender_name,
        "from_email":     sender_email,
        "subject":        data.get("subject", "(no subject)"),
        "preview":        _preview(data.get("body", "")),
        "body":           _clean_html(data.get("body", "")),
        "time":           _format_time(data.get("receivedAt", "")),
        "receivedAt":     data.get("receivedAt", ""),
        "conversationId": data.get("conversationId", ""),
        "unread":         True,
        "source":         "outlook_live",
    }

    prepend_item(INBOX_FILE, email)
    print(f"📧 New email from {email['from']}: {email['subject'][:50]}")
    return {"status": "received", "id": email["id"]}


@app.post("/webhook/teams")
async def receive_teams(request: Request):
    """Receive new Teams message from Power Automate."""
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "detail": "invalid JSON"}

    sender = data.get("from", "Unknown")

    # Filter out messages sent by Ganesh himself
    if MY_NAME.lower() in sender.lower() or MY_EMAIL.lower() in sender.lower():
        print(f"💬 Skipping own Teams message")
        return {"status": "skipped", "reason": "own message"}

    # Filter out system/bot messages
    if not sender or sender.lower() in ("unknown", "system", "bot"):
        return {"status": "skipped", "reason": "system message"}

    msg = {
        "id":        data.get("id", f"teams_{datetime.now().timestamp()}"),
        "from":      sender,
        "message":   _clean_html(data.get("message", "")),
        "teamId":    data.get("teamId", ""),
        "channelId": data.get("channelId", ""),
        "time":      _format_time(data.get("sentAt", "")),
        "sentAt":    data.get("sentAt", ""),
        "unread":    True,
        "source":    "teams_live",
    }

    # Skip empty messages
    if not msg["message"].strip():
        return {"status": "skipped", "reason": "empty message"}

    prepend_item(TEAMS_FILE, msg)
    print(f"💬 New Teams message from {msg['from']}: {msg['message'][:60]}")
    return {"status": "received", "id": msg["id"]}


@app.post("/webhook/calendar")
async def receive_calendar(request: Request):
    """
    Receive calendar event from Power Automate.
    Deduplicates by title + start time to avoid 30x duplicates
    from accept/decline updates.
    """
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "detail": "invalid JSON"}

    event = {
        "id":        data.get("id", f"cal_{datetime.now().timestamp()}"),
        "title":     data.get("title", "Meeting"),
        "start":     data.get("start", ""),
        "end":       data.get("end", ""),
        "attendees": _parse_attendees(data.get("attendees", "")),
        "location":  data.get("location", ""),
        "body":      _clean_html(data.get("body", ""))[:300],
        "time":      _format_calendar_time(data.get("start", "")),
        "duration":  _calc_duration(data.get("start",""), data.get("end","")),
        "goal":      "",
        "source":    "outlook_calendar",
    }

    # ── Deduplication: skip if same title + start time already stored ─────────
    existing = load_json(CALENDAR_FILE)
    dedup_key = f"{event['title']}|{event['start'][:16]}"
    duplicate = any(
        f"{e.get('title','')}|{e.get('start','')[:16]}" == dedup_key
        for e in existing
    )

    if duplicate:
        # Update existing event with latest data (attendees may have changed)
        updated = [
            event if f"{e.get('title','')}|{e.get('start','')[:16]}" == dedup_key
            else e
            for e in existing
        ]
        save_json(CALENDAR_FILE, updated)
        print(f"📅 Updated existing event: {event['title']} at {event['time']}")
        return {"status": "updated", "id": event["id"]}

    prepend_item(CALENDAR_FILE, event)
    print(f"📅 New calendar event: {event['title']} at {event['time']}")
    return {"status": "received", "id": event["id"]}


# ── Admin endpoints ───────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status":   "ok",
        "time":     datetime.now().isoformat(),
        "emails":   len(load_json(INBOX_FILE)),
        "teams":    len(load_json(TEAMS_FILE)),
        "calendar": len(load_json(CALENDAR_FILE)),
    }

@app.get("/data/emails")
async def get_emails():
    return load_json(INBOX_FILE)

@app.get("/data/teams")
async def get_teams():
    return load_json(TEAMS_FILE)

@app.get("/data/calendar")
async def get_calendar():
    return load_json(CALENDAR_FILE)

@app.delete("/data/calendar/clear")
async def clear_calendar():
    """Clear all calendar events — useful after fixing duplicates."""
    save_json(CALENDAR_FILE, [])
    return {"status": "cleared"}

@app.delete("/data/emails/{email_id}/read")
async def mark_email_read(email_id: str):
    emails = load_json(INBOX_FILE)
    for e in emails:
        if e["id"] == email_id:
            e["unread"] = False
    save_json(INBOX_FILE, emails)
    return {"status": "ok"}

@app.post("/data/calendar/dedup")
async def force_dedup_calendar():
    """Manually trigger calendar deduplication."""
    existing = load_json(CALENDAR_FILE)
    cleaned  = dedup_calendar(existing)
    removed  = len(existing) - len(cleaned)
    save_json(CALENDAR_FILE, cleaned)
    return {"status": "ok", "removed": removed, "remaining": len(cleaned)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_email_name(raw: str) -> str:
    if "<" in raw:
        return raw.split("<")[0].strip().strip('"')
    if "@" in raw:
        return raw.split("@")[0].replace(".", " ").title()
    return raw.strip()

def _extract_email_address(raw: str) -> str:
    if "<" in raw and ">" in raw:
        return raw.split("<")[1].split(">")[0].strip()
    if "@" in raw:
        return raw.strip()
    return ""

def _preview(body: str, max_len: int = 120) -> str:
    clean = _clean_html(body)
    clean = " ".join(clean.split())
    return clean[:max_len] + "..." if len(clean) > max_len else clean

def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def _format_time(iso_str: str) -> str:
    if not iso_str:
        return "Just now"
    try:
        dt  = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo)
        diff = now - dt
        if diff.days == 0:
            return dt.strftime("%-I:%M %p")
        elif diff.days == 1:
            return "Yesterday"
        else:
            return dt.strftime("%b %d")
    except Exception:
        return "Just now"

def _format_calendar_time(iso_str: str) -> str:
    if not iso_str:
        return "TBD"
    try:
        # Handle multiple formats Power Automate sends
        clean = iso_str.replace("Z", "+00:00").replace(" ", "T")
        # Remove timezone if fromisoformat can't handle it
        if "+" in clean[10:]:
            clean = clean[:clean.rindex("+")]
        elif clean.endswith("+00:00"):
            clean = clean[:-6]
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%-I:%M %p")
    except Exception:
        # Last resort: try to extract time from string
        import re
        t = re.search(r"T(\d{2}):(\d{2})", iso_str)
        if t:
            h, m = int(t.group(1)), int(t.group(2))
            ampm = "AM" if h < 12 else "PM"
            h = h % 12 or 12
            return f"{h}:{m:02d} {ampm}"
        return "TBD"

def _calc_duration(start: str, end: str) -> str:
    if not start or not end:
        return "?"
    try:
        def _parse(iso):
            clean = iso.replace("Z","").replace(" ","T")
            if "+" in clean[10:]: clean = clean[:clean.rindex("+")]
            return datetime.fromisoformat(clean)
        s    = _parse(start)
        e    = _parse(end)
        mins = int((e - s).total_seconds() / 60)
        if mins < 60:
            return f"{mins} min"
        hours = mins // 60
        rem   = mins % 60
        return f"{hours}h {rem}m" if rem else f"{hours}h"
    except Exception:
        return "?"

def _parse_attendees(raw) -> list[str]:
    if isinstance(raw, list):
        return [_extract_email_name(str(a)) for a in raw]
    if isinstance(raw, str):
        names = re.findall(r'"([^"]+)"', raw) or raw.split(";")
        return [_extract_email_name(n.strip()) for n in names if n.strip()]
    return []


# ── Extension API endpoints ──────────────────────────────────────────────────

@app.post("/api/draft")
async def api_draft(request: Request):
    """
    Called by the Chrome extension to generate a draft.
    Accepts email/chat context, returns draft text.
    """
    try:
        data = await request.json()
    except Exception:
        return {"error": "invalid JSON"}

    platform     = data.get("platform", "email")
    sender       = data.get("sender", "")
    sender_email = data.get("sender_email", "")
    subject      = data.get("subject", "")
    body         = data.get("body", "")
    tone         = data.get("tone", "balanced")
    user_name    = data.get("user_name", "")
    stakeholders = data.get("stakeholders", [])

    if not body:
        return {"error": "No message body provided"}

    user_email = data.get("user_email", data.get("user_name", "default"))

    # ── ReAct agent (deep reasoning) ─────────────────────────────────────────
    try:
        from agent.react_agent import run_react_agent
        result = run_react_agent(
            platform   = platform,
            sender     = sender,
            subject    = subject,
            body       = body,
            user_name  = user_name or "User",
            user_email = user_email,
            tone       = tone.capitalize() if tone else "Balanced",
        )
        return {
            "draft":          result["draft"],
            "critique":       result.get("critique", {}),
            "steps_taken":    result.get("steps_taken", 0),
            "reasoning_trace": result.get("reasoning_trace", []),
            "agent":          "react",
        }
    except Exception as e:
        # Fallback to simple draft if ReAct fails
        pass

    try:
        import sys, uuid
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agent.drafter import generate
        from agent.persona import build_system_prompt

        # Build user config for persona
        user_config = {
            "name":         user_name,
            "title":        data.get("user_title", ""),
            "company":      data.get("user_company", ""),
            "stakeholders": stakeholders,
            "tone":         tone,
        }

        task_type     = "email" if platform in ("gmail", "outlook") else "teams"
        system_prompt = build_system_prompt(task_type, user_config=user_config)

        # Get user_id for per-user corpus (use email if available, else name)
        user_id = data.get("user_email") or user_name.lower().replace(" ","_") or "default"

        # Retrieve relevant history from user's personal corpus
        history_context = ""
        try:
            retriever = Retriever(user_id=user_id)
            results   = retriever.multi_search([
                f"email {sender} conversation history",
                f"{subject} previous discussion",
            ], top_k=3)
            if results:
                history_context = "\n\nRELEVANT PAST CONTEXT:\n"
                history_context += "\n---\n".join(
                    r["text"][:300] for r in results[:3]
                )
        except Exception:
            pass

        user_message  = f"""DRAFT A REPLY FOR {user_name or "the user"}.

MESSAGE TO REPLY TO:
Platform: {platform}
From: {sender} ({sender_email})
Subject / Channel: {subject}
Message body:
{body[:1500]}
{history_context}
INSTRUCTIONS:
- Reply specifically to what {sender} said above
- Reference past context only if directly relevant
- Do NOT invent facts, projects, or commitments not mentioned
- Write as {user_name or "the user"} in first person
- Under 150 words unless detail is clearly needed
"""
        draft  = generate(system_prompt, user_message, temperature=0.7)
        run_id = str(uuid.uuid4())

        return {"draft": draft, "run_id": run_id, "platform": platform}

    except Exception as e:
        return {"error": str(e), "draft": f"[Draft error: {e}]"}


@app.post("/api/ingest-history")
async def api_ingest_history(request: Request):
    """
    Called by the Chrome extension to ingest email/chat history.
    Adds to the user's Chroma corpus for better personalization.
    """
    try:
        data = await request.json()
    except Exception:
        return {"error": "invalid JSON"}

    platform  = data.get("platform", "unknown")
    items     = data.get("items", [])
    user_name = data.get("user_name", "")

    if not items:
        return {"status": "no items", "chunks_added": 0}

    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from ingest.chunker import Chunker
        from ingest.embedder import CorpusStore
        from pathlib import Path as _Path
        _chroma_dir = _Path("data/chroma_db")
        from datetime import datetime

        # Format items as text
        lines = [
            f"TITLE: {platform.upper()} History — {user_name}",
            f"DATE: {datetime.now().isoformat()}",
            "=" * 60, "",
        ]
        for item in items[:100]:
            if platform == "gmail":
                lines += [
                    f"[EMAIL SENT] Subject: {item.get('subject','')}",
                    f"To: {item.get('to','')}",
                    item.get("body", ""),
                    "─" * 40, "",
                ]
            elif platform == "slack":
                lines += [
                    f"[SLACK] #{item.get('channel','')}",
                    item.get("text", ""),
                    "─" * 40, "",
                ]

        # Save to temp file and ingest
        tmp_path = Path(f"data/_extension_{platform}_history.txt")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text("\n".join(lines))

        # Use per-user corpus — isolate each user's data
        ingest_user_id = data.get("user_email") or                          (user_name.lower().replace(" ","_") if user_name else "default")
        chunker = Chunker()
        store   = CorpusStore(chroma_dir=_chroma_dir, user_id=ingest_user_id)
        chunks  = chunker.chunk_file(tmp_path)
        added   = store.add_chunks(chunks)

        print(f"📥 Ingested {len(items)} {platform} items → {added} chunks")
        return {"status": "ok", "chunks_added": added, "items_received": len(items)}

    except Exception as e:
        print(f"Ingest error: {e}")
        return {"error": str(e), "chunks_added": 0}


@app.post("/api/feedback")
async def api_feedback(request: Request):
    """Log feedback and write approved drafts to corpus."""
    """Called by extension to log feedback."""
    try:
        data = await request.json()
    except Exception:
        return {"error": "invalid JSON"}

    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agent.feedback import log_feedback
        log_feedback(
            run_id        = data.get("run_id", ""),
            feedback_type = data.get("type", "thumbs_up"),
            draft         = data.get("draft", ""),
            input_text    = data.get("input", ""),
            task_type     = "extension",
        )
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/health")
async def api_health():
    """Health check for the extension."""
    return {"status": "ok", "service": "Alterus API"}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║   Alterus — Webhook Server v2       ║")
    print("╚══════════════════════════════════════════╝\n")
    print(f"🚀 Starting on http://localhost:{PORT}")
    print(f"📧 Email:    POST /webhook/email")
    print(f"💬 Teams:    POST /webhook/teams")
    print(f"📅 Calendar: POST /webhook/calendar")
    print(f"🏥 Health:   GET  /health")
    print(f"🧹 Dedup:    POST /data/calendar/dedup\n")
    uvicorn.run("channels.webhook_server:app", host="0.0.0.0",
                port=PORT, reload=True)

# ── Clone Score ───────────────────────────────────────────────────────────────
@app.get("/api/clone-score")
async def get_clone_score(user_email: str = "default"):
    try:
        from ingest.embedder import CorpusStore, _safe_collection_name
        from pathlib import Path
        store = CorpusStore(Path("data/chroma_db"), user_id=user_email)
        count = store.count()
        # Score based on corpus size
        if count == 0:
            score = 5
        elif count < 20:
            score = 15
        elif count < 50:
            score = 30
        elif count < 100:
            score = 45
        elif count < 200:
            score = 60
        elif count < 400:
            score = 75
        else:
            score = 90
        return {"score": score, "chunks": count}
    except Exception as e:
        return {"score": 5, "chunks": 0}


# ── PRD Generator ─────────────────────────────────────────────────────────────
@app.post("/api/generate-prd")
async def generate_prd(request: Request):
    data       = await request.json()
    prompt     = data.get("prompt", "")
    user_name  = data.get("user_name", "the user")
    user_email = data.get("user_email", "default")

    if not prompt:
        return {"error": "No prompt provided"}

    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agent.drafter import generate

        system_prompt = f"""You are {user_name}, a senior product manager.
Write a comprehensive PRD in first person as {user_name}.
Format with these sections:
# [Feature Name]

## Overview
## Problem Statement  
## Goals & Success Metrics
## User Stories
## Functional Requirements
## Non-Functional Requirements
## Out of Scope
## Timeline & Milestones

Be specific, actionable, and data-driven. Under 600 words."""

        user_message = f"Write a PRD for: {prompt}"
        prd = generate(system_prompt, user_message, temperature=0.7)
        return {"prd": prd}
    except Exception as e:
        return {"error": str(e)}


# ── AI News ───────────────────────────────────────────────────────────────────
@app.get("/api/ai-news")
async def get_ai_news():
    try:
        import urllib.request
        import xml.etree.ElementTree as ET
        from datetime import datetime

        feeds = [
            ("https://hnrss.org/newest?q=AI+LLM+agent&count=5", "Hacker News"),
            ("https://www.anthropic.com/rss.xml", "Anthropic"),
        ]

        news = []
        for url, source in feeds:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    tree = ET.parse(resp)
                    root = tree.getroot()
                    items = root.findall(".//item")[:3]
                    for item in items:
                        title = item.findtext("title", "").strip()
                        link  = item.findtext("link", "").strip()
                        pub   = item.findtext("pubDate", "").strip()
                        if title and link:
                            news.append({
                                "title":  title[:100],
                                "url":    link,
                                "source": source,
                                "time":   pub[:16] if pub else ""
                            })
            except Exception:
                continue

        # Fallback if feeds fail
        if not news:
            news = [
                {"title": "Anthropic releases Claude 4", "url": "https://anthropic.com", "source": "Anthropic", "time": "Today"},
                {"title": "OpenAI announces GPT-5", "url": "https://openai.com", "source": "OpenAI", "time": "Today"},
                {"title": "Google DeepMind releases Gemini Ultra 2", "url": "https://deepmind.google", "source": "Google", "time": "Today"},
            ]

        return {"news": news[:8]}
    except Exception as e:
        return {"news": [], "error": str(e)}


# ── Zoom Meeting Storage ───────────────────────────────────────────────────────
import json as _json
import re as _re
from pathlib import Path as _Path

_ZOOM_DIR = _Path("data/zoom_meetings")
_ZOOM_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/api/zoom/ingest")
async def zoom_ingest(request: Request):
    data       = await request.json()
    transcript = data.get("transcript", "")
    title      = data.get("title", "Meeting")
    date       = data.get("date", "")
    user_email = data.get("user_email", "default")

    if not transcript:
        return {"error": "No transcript provided"}

    try:
        from agent.drafter import generate

        system_prompt = "You are an expert meeting analyst. Analyze meeting transcripts and extract structured insights. Always respond in valid JSON only, no other text."

        user_message = f"""Analyze this meeting transcript and return a JSON object with these exact keys:
{{
  "summary": "3-4 sentence summary",
  "meeting_sentiment": "productive|neutral|tense|inconclusive",
  "participants": ["name1", "name2"],
  "key_topics": ["topic1", "topic2"],
  "decisions": [{{"decision": "...", "made_by": "..."}}],
  "action_items": [{{"owner": "...", "action": "...", "deadline": "...", "priority": "high|medium|low"}}],
  "followup_email": "full follow-up email draft"
}}

Meeting title: {title}
Date: {date}

Transcript:
{transcript[:4000]}

Return ONLY the JSON object."""

        raw = generate(system_prompt, user_message, temperature=0.3)

        json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if json_match:
            result = _json.loads(json_match.group())
        else:
            result = _json.loads(raw.strip())

        result["id"]                 = f"{user_email}_{date}_{title[:20]}".replace(" ", "_")
        result["meeting_title"]      = title
        result["meeting_date"]       = date
        result["user_email"]         = user_email
        result["transcript_preview"] = transcript[:500]

        safe_id  = _re.sub(r"[^a-zA-Z0-9_-]", "_", result["id"])[:80]
        out_path = _ZOOM_DIR / f"{safe_id}.json"
        out_path.write_text(_json.dumps(result, indent=2))

        # Write meeting to corpus for future context
        try:
            from agent.memory_writeback import write_zoom_to_corpus
            write_zoom_to_corpus(user_email, result)
        except Exception:
            pass

        return {"success": True, "meeting_id": result["id"], "title": title}

    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/zoom/meetings")
async def zoom_meetings(user_email: str = "default"):
    try:
        meetings = []
        for f in sorted(_ZOOM_DIR.glob("*.json"), reverse=True):
            try:
                m = _json.loads(f.read_text())
                if m.get("user_email") == user_email or user_email == "default":
                    meetings.append(m)
            except Exception:
                continue
        return {"meetings": meetings[:20]}
    except Exception as e:
        return {"meetings": [], "error": str(e)}

# ── Self-Healing Endpoints ────────────────────────────────────────────────────
@app.post("/api/healer/run")
async def run_healer(request: Request):
    """Run the self-healing batch analyzer."""
    try:
        data        = await request.json()
        auto_apply  = data.get("auto_apply", False)
        from agent.self_healer import run_batch_healer
        report = run_batch_healer(auto_apply=auto_apply)
        return report
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/healer/feedback")
async def log_healer_feedback(request: Request):
    """Log individual feedback entry for self-healing."""
    try:
        data = await request.json()
        from agent.self_healer import save_feedback
        entry = {
            "timestamp":     __import__("datetime").datetime.now().isoformat(),
            "feedback_type": data.get("feedback_type", ""),
            "platform":      data.get("platform", "email"),
            "task_type":     data.get("task_type", "draft"),
            "draft":         data.get("draft", ""),
            "edited_draft":  data.get("edited_draft", ""),
            "input_text":    data.get("input_text", ""),
            "user_email":    data.get("user_email", "default"),
        }
        save_feedback(entry)
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/healer/report")
async def get_healer_report():
    """Get the latest self-healer report."""
    try:
        from agent.self_healer import HEALER_REPORT
        if HEALER_REPORT.exists():
            import json as _j
            return _j.loads(HEALER_REPORT.read_text())
        return {"error": "No report yet. Run the healer first."}
    except Exception as e:
        return {"error": str(e)}

# ── Daily Briefing ────────────────────────────────────────────────────────────
@app.post("/api/briefing")
async def daily_briefing(request: Request):
    """Generate a personalized daily briefing in first person."""
    try:
        data       = await request.json()
        user_name  = data.get("user_name", "")
        user_email = data.get("user_email", "default")
        agenda     = data.get("agenda", "")
        date       = data.get("date", "")

        from agent.drafter import generate

        system_prompt = f"""You are {user_name}. Write a daily briefing to yourself in first person.
Be direct, specific, and actionable. Sound like a smart chief of staff briefing themselves.
Under 120 words. No headers. No markdown. Plain flowing sentences."""

        user_message = f"""Today is {date}.

My agenda: {agenda if agenda else "No agenda provided — use general priorities"}

Write my daily briefing in this exact format:

**Today's Priorities:**
- [Priority 1 — specific and actionable]
- [Priority 2 — specific and actionable]  
- [Priority 3 — specific and actionable]

**Needs Response:** [Name 1], [Name 2] — or "None today"

**Risk Watch:** [One specific risk in one sentence]

**Mindset:** [One motivational sentence]

Use bullet points. First person. Under 120 words total. Be specific — use real names and projects from agenda if provided."""

        briefing = generate(system_prompt, user_message, temperature=0.8)
        return {"briefing": briefing}

    except Exception as e:
        return {"error": str(e)}

# ── Stakeholder Intelligence ──────────────────────────────────────────────────
@app.post("/api/stakeholders/analyze")
async def analyze_stakeholders(request: Request):
    """Analyze corpus to build stakeholder profiles."""
    try:
        data               = await request.json()
        user_email         = data.get("user_email", "default")
        known_stakeholders = data.get("stakeholders", [])

        from agent.stakeholder_intelligence import analyze_stakeholders
        profiles = analyze_stakeholders(user_email, known_stakeholders or None)
        return {"profiles": profiles, "count": len(profiles)}
    except Exception as e:
        return {"error": str(e), "profiles": []}


@app.get("/api/stakeholders/profiles")
async def get_stakeholder_profiles(user_email: str = "default"):
    """Get all stakeholder profiles for a user."""
    try:
        from agent.stakeholder_intelligence import get_all_profiles
        profiles = get_all_profiles(user_email)
        return {"profiles": profiles}
    except Exception as e:
        return {"profiles": [], "error": str(e)}


@app.post("/api/stakeholders/update")
async def update_stakeholder_profile(request: Request):
    """Update stakeholder profile after approved draft."""
    try:
        data             = await request.json()
        user_email       = data.get("user_email", "default")
        stakeholder_name = data.get("stakeholder_name", "")
        draft            = data.get("draft", "")
        feedback         = data.get("feedback", "approved")
        context          = data.get("context", "")

        from agent.stakeholder_intelligence import update_profile_from_draft
        success = update_profile_from_draft(
            user_email, stakeholder_name, draft, feedback, context
        )
        return {"success": success}
    except Exception as e:
        return {"error": str(e)}

# ── Inbox Activity Feed ───────────────────────────────────────────────────────
@app.get("/api/inbox/activity")
async def inbox_activity(user_email: str = "default"):
    """
    Returns real draft activity from extension usage.
    Pulled from feedback_log.json — shows Gmail/Outlook/Teams separately.
    """
    try:
        from agent.self_healer import load_feedback
        feedback = load_feedback()

        # Filter by user
        user_feedback = [
            f for f in feedback
            if f.get("user_email", "default") == user_email
            or user_email == "default"
        ]

        # Group by platform
        gmail   = []
        outlook = []
        teams   = []
        other   = []

        for f in reversed(user_feedback):  # most recent first
            platform = f.get("platform", "email").lower()
            entry = {
                "timestamp":     f.get("timestamp", ""),
                "sender":        f.get("sender", f.get("input_text", "")[:40]),
                "subject":       f.get("subject", f.get("task_type", "Draft")),
                "draft_snippet": f.get("draft", "")[:120],
                "feedback":      f.get("feedback_type", ""),
                "platform":      platform,
                "task_type":     f.get("task_type", "draft"),
                "input_snippet": f.get("input_text", "")[:100],
            }

            if "gmail" in platform or (platform == "email" and "gmail" in f.get("source", "")):
                gmail.append(entry)
            elif "outlook" in platform or "office" in platform:
                outlook.append(entry)
            elif "teams" in platform or "slack" in platform:
                teams.append(entry)
            else:
                other.append(entry)

        return {
            "gmail":   gmail[:10],
            "outlook": outlook[:10],
            "teams":   teams[:10],
            "other":   other[:5],
            "total":   len(user_feedback),
        }

    except Exception as e:
        return {"gmail": [], "outlook": [], "teams": [], "other": [], "total": 0, "error": str(e)}
