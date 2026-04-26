"""
webhook_server.py  (v3 — Graph API native, no Power Automate)
FastAPI server receiving Microsoft Graph Change Notifications for:
  - Outlook email  (push — Graph posts here on new email)
  - Calendar events (push — Graph posts here on new/updated event)
  - Teams messages  (poll — fetched on-demand, no push in v1)

REPLACES: Power Automate + ngrok entirely.

HOW GRAPH NOTIFICATIONS WORK:
  1. User connects Outlook (OAuth)
  2. outlook_connector.py registers subscriptions with Graph
  3. Graph validates our endpoint by POSTing with ?validationToken=<token>
     → we respond 200 with plain text token (Graph confirms we own this URL)
  4. From then on, Graph POSTs a notification here on every new email/event
  5. Notification contains the message ID (not full content)
  6. We fetch the full message from Graph using the user's access token
  7. We process, prioritize, store — dashboard updates

WHAT USERS DO: Nothing. Connect Outlook once. Done.
"""

import sys
import json
import re
import os
import asyncio
import httpx
import secrets as _secrets
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR      = Path("data")
INBOX_FILE    = DATA_DIR / "live_emails.json"
TEAMS_FILE    = DATA_DIR / "live_teams.json"
CALENDAR_FILE = DATA_DIR / "live_calendar.json"
PORT          = 8000

MY_NAME  = os.getenv("USER_DISPLAY_NAME", "Ganesh Srinivasan")
MY_EMAIL = os.getenv("USER_EMAIL", "ganesh.srinivasan@servicenow.com")

app = FastAPI(title="Alterus Webhook Server v3")

from channels.outlook_connector import router as outlook_router
app.include_router(outlook_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Startup: kick off subscription renewal loop ───────────────────────────────

@app.on_event("startup")
async def startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Clean calendar duplicates from old data
    existing = load_json(CALENDAR_FILE)
    if existing:
        cleaned = dedup_calendar(existing)
        if len(cleaned) < len(existing):
            save_json(CALENDAR_FILE, cleaned)
            print(f"🧹 Cleaned {len(existing) - len(cleaned)} duplicate calendar events")

    # Start background subscription renewal (keeps Graph subscriptions alive)
    asyncio.create_task(_run_renewal_loop())
    print(f"🚀 Alterus webhook server v3 ready — Graph API native, no Power Automate")


async def _run_renewal_loop():
    """Keeps Graph subscriptions from expiring. Runs every hour."""
    await asyncio.sleep(10)   # brief delay after startup
    from channels.graph_subscriptions import subscription_renewal_loop
    await subscription_renewal_loop()


# ── Graph webhook endpoints ───────────────────────────────────────────────────
#
# IMPORTANT: Graph uses a two-step protocol for every webhook endpoint:
#
#   Step 1 — Validation (happens once when subscription is registered):
#     Graph sends POST with query param:  ?validationToken=<encoded_token>
#     We must respond:  200, Content-Type: text/plain, body = token (plain text)
#     This proves we own the URL.
#
#   Step 2 — Notifications (happens on every new email/event):
#     Graph sends POST with JSON body:
#     { "value": [{ "subscriptionId", "clientState", "changeType", "resourceData": { "id" } }] }
#     clientState = user_email (we set this when registering)
#     resourceData.id = the message/event ID → we fetch full object from Graph

@app.post("/webhook/email")
async def receive_email(request: Request, background_tasks: BackgroundTasks):
    """
    Receives Graph change notifications for new inbox emails.
    Replaces the old Power Automate → /webhook/email flow entirely.
    """
    # ── Step 1: Graph validation handshake ───────────────────────────────────
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        # Graph is validating our endpoint — respond with token as plain text
        print("📡 Graph validating email webhook endpoint...")
        return Response(
            content=validation_token,
            media_type="text/plain",
            status_code=200,
        )

    # ── Step 2: Process notification ─────────────────────────────────────────
    try:
        raw_body = await request.body()
        data     = json.loads(raw_body)
    except Exception:
        return Response(status_code=400)

    # Graph expects a fast 202 response — do heavy work in background
    for notification in data.get("value", []):
        background_tasks.add_task(_process_email_notification, notification)

    # Respond 202 immediately — Graph will retry if we don't respond quickly
    return Response(status_code=202)


async def _process_email_notification(notification: dict):
    """
    Background task: fetch full email from Graph and store it.

    Graph notifications contain only the message ID, not the content.
    We fetch the full message using the user's stored access token.
    """
    user_email = notification.get("clientState", "")
    if not user_email:
        # Try to look up by subscription ID
        sub_id = notification.get("subscriptionId", "")
        from channels.graph_subscriptions import get_user_email_for_subscription
        user_email = get_user_email_for_subscription(sub_id) or ""

    if not user_email:
        print("⚠️  Email notification: could not identify user")
        return

    message_id = (notification.get("resourceData") or {}).get("id", "")
    if not message_id:
        print("⚠️  Email notification: no message ID")
        return

    # Get access token for this user
    from channels.outlook_connector import get_valid_access_token
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        print(f"⚠️  Email notification: no valid token for {user_email}")
        return

    # Fetch full message from Graph
    url = (
        f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
        "?$select=id,subject,from,body,bodyPreview,receivedDateTime,"
        "isRead,conversationId,importance,toRecipients"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code != 200:
        print(f"⚠️  Could not fetch message {message_id}: {resp.status_code}")
        return

    msg    = resp.json()
    sender = msg.get("from", {}).get("emailAddress", {})

    sender_email = sender.get("address", "")
    sender_name  = sender.get("name", sender_email)

    # Skip own messages
    if MY_EMAIL.lower() in sender_email.lower():
        return

    email = {
        "id":             msg.get("id"),
        "from":           sender_name,
        "from_email":     sender_email,
        "subject":        msg.get("subject", "(no subject)"),
        "preview":        msg.get("bodyPreview", "")[:150],
        "body":           _clean_html(msg.get("body", {}).get("content", "")),
        "time":           _format_time(msg.get("receivedDateTime", "")),
        "receivedAt":     msg.get("receivedDateTime", ""),
        "conversationId": msg.get("conversationId", ""),
        "unread":         True,
        "importance":     msg.get("importance", "normal"),
        "user_email":     user_email,   # which user this belongs to
        "source":         "graph_push", # distinguishes from on-demand fetch
    }

    prepend_item(INBOX_FILE, email)
    print(f"📧 New email via Graph push → {user_email}: {email['subject'][:50]}")

    # ── Optionally: trigger prioritizer immediately ───────────────────────────
    # Uncomment when agent/prioritizer.py is ready for real-time scoring:
    # try:
    #     from agent.prioritizer import score_email
    #     email["priority_score"] = score_email(email)
    # except Exception:
    #     pass


@app.post("/webhook/calendar")
async def receive_calendar(request: Request, background_tasks: BackgroundTasks):
    """
    Receives Graph change notifications for calendar events.
    Handles both new events (created) and updates (updated).
    """
    # ── Graph validation handshake ────────────────────────────────────────────
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        print("📡 Graph validating calendar webhook endpoint...")
        return Response(content=validation_token, media_type="text/plain", status_code=200)

    try:
        raw_body = await request.body()
        data     = json.loads(raw_body)
    except Exception:
        return Response(status_code=400)

    for notification in data.get("value", []):
        background_tasks.add_task(_process_calendar_notification, notification)

    return Response(status_code=202)


async def _process_calendar_notification(notification: dict):
    """Fetch full calendar event from Graph and store/update it."""
    user_email = notification.get("clientState", "")
    if not user_email:
        sub_id = notification.get("subscriptionId", "")
        from channels.graph_subscriptions import get_user_email_for_subscription
        user_email = get_user_email_for_subscription(sub_id) or ""

    if not user_email:
        return

    event_id = (notification.get("resourceData") or {}).get("id", "")
    if not event_id:
        return

    from channels.outlook_connector import get_valid_access_token
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return

    url = (
        f"https://graph.microsoft.com/v1.0/me/events/{event_id}"
        "?$select=id,subject,start,end,location,organizer,attendees,"
        "isOnlineMeeting,onlineMeetingUrl,bodyPreview"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    if resp.status_code != 200:
        return

    e = resp.json()
    event = {
        "id":          e.get("id"),
        "title":       e.get("subject", "(no title)"),
        "start":       e.get("start", {}).get("dateTime", ""),
        "end":         e.get("end", {}).get("dateTime", ""),
        "location":    e.get("location", {}).get("displayName", ""),
        "organizer":   e.get("organizer", {}).get("emailAddress", {}).get("name", ""),
        "isOnlineMtg": e.get("isOnlineMeeting", False),
        "joinUrl":     e.get("onlineMeetingUrl", ""),
        "preview":     e.get("bodyPreview", "")[:200],
        "attendees":   [
            a.get("emailAddress", {}).get("name", "")
            for a in e.get("attendees", [])
        ],
        "time":        _format_time(e.get("start", {}).get("dateTime", "")),
        "user_email":  user_email,
        "source":      "graph_push",
    }

    # Upsert: update if exists, insert if new
    events   = load_json(CALENDAR_FILE)
    dedup_key = f"{event['title']}|{event['start'][:16]}"
    existing_keys = {f"{x.get('title','')}|{x.get('start','')[:16]}" for x in events}

    if dedup_key in existing_keys:
        events = [
            event if f"{x.get('title','')}|{x.get('start','')[:16]}" == dedup_key
            else x for x in events
        ]
        save_json(CALENDAR_FILE, events)
        print(f"📅 Updated event via Graph push: {event['title']}")
    else:
        prepend_item(CALENDAR_FILE, event)
        print(f"📅 New event via Graph push: {event['title']} at {event['start'][:16]}")


# ── Existing dashboard API endpoints (unchanged) ──────────────────────────────

@app.get("/health")
async def health():
    from channels.graph_subscriptions import _load_subs
    subs = _load_subs()
    return {
        "status":        "ok",
        "time":          datetime.now().isoformat(),
        "emails":        len(load_json(INBOX_FILE)),
        "calendar":      len(load_json(CALENDAR_FILE)),
        "teams":         len(load_json(TEAMS_FILE)),
        "subscriptions": len(subs),
        "mode":          "graph_native",
    }

@app.get("/api/health")
async def api_health():
    return {"status": "ok", "service": "alterus-webhook-server-v3"}

@app.get("/data/emails")
async def get_emails(user_email: str = "default"):
    emails = load_json(INBOX_FILE)
    if user_email != "default":
        emails = [e for e in emails if e.get("user_email", "default") == user_email]
    return emails

@app.get("/data/calendar")
async def get_calendar_data(user_email: str = "default"):
    events = load_json(CALENDAR_FILE)
    if user_email != "default":
        events = [e for e in events if e.get("user_email", "default") == user_email]
    return events

@app.get("/api/subscriptions")
async def get_subscriptions(user_email: str = "default"):
    """Check which Graph subscriptions are active for a user."""
    from channels.graph_subscriptions import get_subscriptions_for_user
    subs = get_subscriptions_for_user(user_email)
    return {"subscriptions": subs, "count": len(subs)}

@app.post("/api/subscriptions/refresh")
async def refresh_subscriptions(request: Request):
    """Manually re-register subscriptions — useful after a disconnect/reconnect."""
    data         = await request.json()
    user_email   = data.get("user_email", "default")

    from channels.outlook_connector import get_valid_access_token
    from channels.graph_subscriptions import register_all_subscriptions

    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    results = await register_all_subscriptions(user_email, access_token)
    return results

@app.delete("/data/emails/clear")
async def clear_emails():
    save_json(INBOX_FILE, [])
    return {"status": "cleared"}

@app.delete("/data/calendar/clear")
async def clear_calendar():
    save_json(CALENDAR_FILE, [])
    return {"status": "cleared"}


# ── All other existing endpoints kept as-is ───────────────────────────────────
# (draft, feedback, briefing, stakeholders, risk engine, etc.)
# Only the webhook handlers and startup changed.
# Paste your remaining endpoints from the original webhook_server.py below.


def verify_request(request) -> bool:
    """Auth check for dashboard API endpoints (unchanged)."""
    import base64
    if request.url.path in ("/api/health", "/health"):
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            decoded = base64.b64decode(auth[7:] + "==").decode("utf-8")
            if decoded.startswith("alterus:") and "@" in decoded:
                return True
        except Exception:
            pass
    origin  = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    allowed = ["https://app.alterus.io", "https://alterus-app.netlify.app", "http://localhost:3000"]
    if any(o in origin or o in referer for o in allowed):
        return True
    api_secret = os.getenv("ALTERUS_API_SECRET", "")
    if auth.startswith("Bearer ") and api_secret:
        return _secrets.compare_digest(auth[7:], api_secret)
    return not api_secret


# ── Storage helpers (unchanged) ───────────────────────────────────────────────

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
    items = load_json(path)
    items = [x for x in items if x.get("id") != item.get("id")]
    items.insert(0, item)
    save_json(path, items[:max_items])

def dedup_calendar(events: list) -> list:
    seen = {}
    for event in events:
        key = f"{event.get('title','')}|{event.get('start','')[:16]}"
        if key not in seen:
            seen[key] = event
    return list(seen.values())


# ── Text helpers (unchanged) ──────────────────────────────────────────────────

def _clean_html(html: str) -> str:
    if not html: return ""
    text = re.sub(r"<[^>]+>", " ", html)
    for entity, char in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()

def _format_time(iso_str: str) -> str:
    if not iso_str: return "Just now"
    try:
        dt   = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now  = datetime.now(dt.tzinfo)
        diff = now - dt
        if diff.days == 0:   return dt.strftime("%-I:%M %p")
        elif diff.days == 1: return "Yesterday"
        else:                return dt.strftime("%b %d")
    except Exception:
        return "Just now"

def _extract_email_name(raw: str) -> str:
    if "<" in raw: return raw.split("<")[0].strip().strip('"')
    if "@" in raw: return raw.split("@")[0].replace(".", " ").title()
    return raw.strip()

def _extract_email_address(raw: str) -> str:
    if "<" in raw and ">" in raw: return raw.split("<")[1].split(">")[0].strip()
    if "@" in raw: return raw.strip()
    return ""

def _parse_attendees(raw) -> list:
    if isinstance(raw, list): return raw
    if isinstance(raw, str) and raw:
        return [a.strip() for a in re.split(r"[;,]", raw) if a.strip()]
    return []


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("channels.webhook_server:app", host="0.0.0.0", port=PORT, reload=True)
