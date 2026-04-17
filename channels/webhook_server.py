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

    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agent.drafter import generate
        from agent.persona import build_system_prompt

        tone_instruction = {
            "direct":     "Be direct, concise, and confident. No filler phrases.",
            "balanced":   "Be professional and clear. Direct but not blunt.",
            "diplomatic": "Be warm, considerate, and thoughtful in tone.",
        }.get(tone, "Be professional and clear.")

        system_prompt = build_system_prompt("email") if platform in ("gmail", "outlook") else build_system_prompt("teams")

        user_message = f"""
You are drafting a reply for {user_name or "the user"}.

MESSAGE TO REPLY TO:
Platform: {platform}
From: {sender} ({sender_email})
Subject: {subject}
Message: {body[:1500]}

TONE: {tone_instruction}

Write a reply in {user_name or "the user"}'s voice.
Be specific to what {sender} actually said.
Do not invent facts or projects not mentioned above.
Under 150 words unless the message requires more detail.
"""
        draft = generate(system_prompt, user_message, temperature=0.7)

        import uuid
        run_id = str(uuid.uuid4())

        return {
            "draft":    draft,
            "run_id":   run_id,
            "platform": platform,
        }

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

        chunker = Chunker()
        store   = CorpusStore()
        chunks  = chunker.chunk_file(tmp_path)
        added   = store.add_chunks(chunks)

        print(f"📥 Ingested {len(items)} {platform} items → {added} chunks")
        return {"status": "ok", "chunks_added": added, "items_received": len(items)}

    except Exception as e:
        print(f"Ingest error: {e}")
        return {"error": str(e), "chunks_added": 0}


@app.post("/api/feedback")
async def api_feedback(request: Request):
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
