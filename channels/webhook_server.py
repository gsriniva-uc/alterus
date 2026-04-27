"""
webhook_server.py  (v3 — Graph API native, all endpoints restored)
FastAPI server for Alterus.

Webhook receivers (Graph push notifications):
  POST /webhook/email       ← Graph pushes new emails here
  POST /webhook/calendar    ← Graph pushes calendar events here

Agent API (called by React app):
  POST /api/draft           ← Draft Reply button
  POST /api/generate-prd    ← PRD generator
  POST /api/briefing        ← Daily briefing
  POST /api/feedback        ← Thumbs up/down
  POST /api/ingest-history  ← Corpus ingest
  GET  /api/clone-score     ← Corpus size score
  GET  /api/ai-news         ← AI news feed

Zoom:
  POST /api/zoom/ingest     ← Analyze transcript
  GET  /api/zoom/meetings   ← List meetings

Self-healer:
  POST /api/healer/run
  POST /api/healer/feedback
  GET  /api/healer/report

Stakeholders:
  POST /api/stakeholders/analyze
  GET  /api/stakeholders/profiles
  POST /api/stakeholders/update

Inbox:
  GET  /api/inbox/activity

Risk:
  POST /api/risk/analyze
  GET  /api/risk/summary

Data:
  GET  /data/emails
  GET  /data/calendar
  GET  /health
  GET  /api/health
  GET  /api/subscriptions
  POST /api/subscriptions/refresh
"""

import sys
import json
import re
import os
import asyncio
import httpx
import secrets as _secrets
import json as _json
import re as _re
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR      = Path("data")
INBOX_FILE    = DATA_DIR / "live_emails.json"
TEAMS_FILE    = DATA_DIR / "live_teams.json"
CALENDAR_FILE = DATA_DIR / "live_calendar.json"
PORT          = 8000

MY_NAME  = os.getenv("USER_DISPLAY_NAME", "Ganesh Srinivasan")
MY_EMAIL = os.getenv("USER_EMAIL", "ganesh.srinivasan@servicenow.com")

_ZOOM_DIR = Path("data/zoom_meetings")

app = FastAPI(title="Alterus Webhook Server v3")

from channels.outlook_connector import router as outlook_router
app.include_router(outlook_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _ZOOM_DIR.mkdir(parents=True, exist_ok=True)

    # Clean calendar duplicates
    existing = load_json(CALENDAR_FILE)
    if existing:
        cleaned = dedup_calendar(existing)
        if len(cleaned) < len(existing):
            save_json(CALENDAR_FILE, cleaned)
            print(f"🧹 Cleaned {len(existing) - len(cleaned)} duplicate calendar events")

    # Start subscription renewal loop
    asyncio.create_task(_run_renewal_loop())
    print(f"🚀 Alterus webhook server v3 ready — Graph API native")


async def _run_renewal_loop():
    await asyncio.sleep(10)
    from channels.graph_subscriptions import subscription_renewal_loop
    await subscription_renewal_loop()


# ── Auth helper ───────────────────────────────────────────────────────────────

def verify_request(request) -> bool:
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
    allowed = [
        "https://app.alterus.io",
        "https://alterus-app.netlify.app",
        "http://localhost:3000",
    ]
    if any(o in origin or o in referer for o in allowed):
        return True
    api_secret = os.getenv("ALTERUS_API_SECRET", "")
    if auth.startswith("Bearer ") and api_secret:
        return _secrets.compare_digest(auth[7:], api_secret)
    return not api_secret


# ════════════════════════════════════════════════════════════════
# GRAPH WEBHOOK ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.post("/webhook/email")
async def receive_email(request: Request, background_tasks: BackgroundTasks):
    """
    Receives Microsoft Graph change notifications for new inbox emails.
    Step 1: Graph sends ?validationToken → respond with plain text (one-time handshake)
    Step 2: Graph sends notification JSON → fetch full message in background
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        print("📡 Graph validating /webhook/email endpoint")
        return Response(content=validation_token, media_type="text/plain", status_code=200)

    try:
        raw_body = await request.body()
        data     = json.loads(raw_body)
    except Exception:
        return Response(status_code=400)

    for notification in data.get("value", []):
        background_tasks.add_task(_process_email_notification, notification)

    return Response(status_code=202)


async def _process_email_notification(notification: dict):
    """Fetch full email from Graph and store it."""
    user_email = notification.get("clientState", "")
    if not user_email:
        from channels.graph_subscriptions import get_user_email_for_subscription
        user_email = get_user_email_for_subscription(
            notification.get("subscriptionId", "")
        ) or ""
    if not user_email:
        return

    message_id = (notification.get("resourceData") or {}).get("id", "")
    if not message_id:
        return

    from channels.outlook_connector import get_valid_access_token
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return

    url = (
        f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
        "?$select=id,subject,from,body,bodyPreview,receivedDateTime,"
        "isRead,conversationId,importance,toRecipients"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    if resp.status_code != 200:
        return

    msg          = resp.json()
    sender       = msg.get("from", {}).get("emailAddress", {})
    sender_email = sender.get("address", "")
    sender_name  = sender.get("name", sender_email)

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
        "user_email":     user_email,
        "source":         "graph_push",
    }

    prepend_item(INBOX_FILE, email)
    print(f"📧 New email via Graph push → {user_email}: {email['subject'][:50]}")


@app.post("/webhook/calendar")
async def receive_calendar(request: Request, background_tasks: BackgroundTasks):
    """Receives Graph change notifications for calendar events."""
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        print("📡 Graph validating /webhook/calendar endpoint")
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
    """Fetch full calendar event from Graph and store it."""
    user_email = notification.get("clientState", "")
    if not user_email:
        from channels.graph_subscriptions import get_user_email_for_subscription
        user_email = get_user_email_for_subscription(
            notification.get("subscriptionId", "")
        ) or ""
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
        "attendees":   [a.get("emailAddress", {}).get("name", "") for a in e.get("attendees", [])],
        "time":        _format_time(e.get("start", {}).get("dateTime", "")),
        "user_email":  user_email,
        "source":      "graph_push",
    }

    events    = load_json(CALENDAR_FILE)
    dedup_key = f"{event['title']}|{event['start'][:16]}"
    existing_keys = {f"{x.get('title','')}|{x.get('start','')[:16]}" for x in events}

    if dedup_key in existing_keys:
        events = [
            event if f"{x.get('title','')}|{x.get('start','')[:16]}" == dedup_key
            else x for x in events
        ]
        save_json(CALENDAR_FILE, events)
    else:
        prepend_item(CALENDAR_FILE, event)
    print(f"📅 Calendar event via Graph push: {event['title'][:50]}")


# ════════════════════════════════════════════════════════════════
# DRAFT ENDPOINT  ← fixes the broken Draft Reply button
# ════════════════════════════════════════════════════════════════

@app.post("/api/draft")
async def api_draft(request: Request):
    """
    Called by InboxTab.js draftReply() — the Draft Reply button.

    Payload:  { platform, body, subject, sender, sender_email,
                user_name, user_email, tone }
    Returns:  { draft, agent, critique? }
    """
    if not verify_request(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    platform     = data.get("platform", "email")
    body         = data.get("body", "")
    subject      = data.get("subject", "")
    sender       = data.get("sender", "")
    sender_email = data.get("sender_email", "")
    tone         = data.get("tone", "balanced")
    user_name    = data.get("user_name", "")
    user_email   = data.get("user_email", "default")
    stakeholders = data.get("stakeholders", [])

    if not body:
        return JSONResponse({"error": "No message body provided"}, status_code=400)

    # ── Try ReAct agent first (full reasoning + retrieval) ────────────────────
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
        draft = result.get("draft") or result.get("final_draft", "")
        if draft and len(draft.strip()) > 10:
            return {
                "draft":           draft,
                "agent":           "react",
                "critique":        result.get("critique", {}),
                "steps_taken":     result.get("steps_taken", 0),
                "reasoning_trace": result.get("reasoning_trace", []),
            }
    except Exception as e:
        print(f"⚠️  ReAct agent failed ({e}), falling back to direct drafter")

    # ── Fallback: direct drafter with retrieval ───────────────────────────────
    try:
        from agent.drafter import generate
        from agent.persona import build_system_prompt

        task_type     = "email" if platform in ("gmail", "outlook", "email") else "teams"
        user_config   = {
            "name":         user_name,
            "tone":         tone,
            "stakeholders": stakeholders,
        }
        system_prompt = build_system_prompt(task_type, user_config=user_config)

        # Try to pull relevant context from Chroma
        history_context = ""
        try:
            from retrieval.retriever import Retriever
            retriever = Retriever(user_id=user_email)
            results   = retriever.multi_search([
                f"email {sender} conversation history",
                f"{subject} previous discussion",
            ], top_k=3)
            if results:
                history_context = "\n\nRELEVANT PAST CONTEXT:\n"
                history_context += "\n---\n".join(r["text"][:300] for r in results[:3])
        except Exception:
            pass

        tone_instruction = {
            "formal":       "Use a formal, executive-level tone.",
            "professional": "Use a professional, direct tone.",
            "casual":       "Use a conversational, friendly tone.",
            "concise":      "Be extremely concise — under 60 words.",
            "balanced":     "Use a professional but approachable tone.",
        }.get(tone, "Use a professional, direct tone.")

        user_message = f"""Draft a reply to this {platform} message.

From: {sender} <{sender_email}>
Subject: {subject}
Message: {body[:1500]}
{history_context}

Tone: {tone_instruction}
Write only the reply body. Sound exactly like {user_name or 'the user'}. Under 150 words."""

        draft = generate(system_prompt, user_message, temperature=0.7)
        return {"draft": draft, "agent": "drafter_direct", "platform": platform}

    except Exception as e:
        print(f"❌ /api/draft error: {e}")
        return JSONResponse({"error": str(e), "draft": ""}, status_code=500)


# ════════════════════════════════════════════════════════════════
# HEALTH & DATA ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    try:
        from channels.graph_subscriptions import _load_subs
        subs = _load_subs()
    except Exception:
        subs = {}
    return {
        "status":        "ok",
        "time":          datetime.now().isoformat(),
        "emails":        len(load_json(INBOX_FILE)),
        "calendar":      len(load_json(CALENDAR_FILE)),
        "subscriptions": len(subs),
        "mode":          "graph_native_v3",
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

@app.get("/data/teams")
async def get_teams():
    return load_json(TEAMS_FILE)

@app.get("/data/calendar")
async def get_calendar_data(user_email: str = "default"):
    events = load_json(CALENDAR_FILE)
    if user_email != "default":
        events = [e for e in events if e.get("user_email", "default") == user_email]
    return events

@app.delete("/data/emails/clear")
async def clear_emails():
    save_json(INBOX_FILE, [])
    return {"status": "cleared"}

@app.delete("/data/calendar/clear")
async def clear_calendar():
    save_json(CALENDAR_FILE, [])
    return {"status": "cleared"}

@app.post("/data/calendar/dedup")
async def force_dedup_calendar():
    existing = load_json(CALENDAR_FILE)
    cleaned  = dedup_calendar(existing)
    removed  = len(existing) - len(cleaned)
    save_json(CALENDAR_FILE, cleaned)
    return {"status": "ok", "removed": removed, "remaining": len(cleaned)}

@app.delete("/data/emails/{email_id}/read")
async def mark_email_read(email_id: str):
    emails = load_json(INBOX_FILE)
    for e in emails:
        if e["id"] == email_id:
            e["unread"] = False
    save_json(INBOX_FILE, emails)
    return {"status": "ok"}

@app.get("/api/subscriptions")
async def get_subscriptions(user_email: str = "default"):
    from channels.graph_subscriptions import get_subscriptions_for_user
    subs = get_subscriptions_for_user(user_email)
    return {"subscriptions": subs, "count": len(subs)}

@app.post("/api/subscriptions/refresh")
async def refresh_subscriptions(request: Request):
    data         = await request.json()
    user_email   = data.get("user_email", "default")
    from channels.outlook_connector import get_valid_access_token
    from channels.graph_subscriptions import register_all_subscriptions
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected"}, status_code=401)
    results = await register_all_subscriptions(user_email, access_token)
    return results


# ════════════════════════════════════════════════════════════════
# CLONE SCORE
# ════════════════════════════════════════════════════════════════

@app.get("/api/clone-score")
async def get_clone_score(user_email: str = "default"):
    try:
        from ingest.embedder import CorpusStore
        store = CorpusStore(Path("data/chroma_db"), user_id=user_email)
        count = store.count()
        if count == 0:       score = 5
        elif count < 20:     score = 15
        elif count < 50:     score = 30
        elif count < 100:    score = 45
        elif count < 200:    score = 60
        elif count < 400:    score = 75
        else:                score = 90
        return {"score": score, "chunks": count}
    except Exception:
        return {"score": 5, "chunks": 0}


# ════════════════════════════════════════════════════════════════
# PRD GENERATOR
# ════════════════════════════════════════════════════════════════

@app.post("/api/generate-prd")
async def generate_prd(request: Request):
    data      = await request.json()
    prompt    = data.get("prompt", "")
    user_name = data.get("user_name", "the user")
    if not prompt:
        return {"error": "No prompt provided"}
    try:
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
        prd = generate(system_prompt, f"Write a PRD for: {prompt}", temperature=0.7)
        return {"prd": prd}
    except Exception as e:
        return {"error": str(e)}


# ════════════════════════════════════════════════════════════════
# DAILY BRIEFING
# ════════════════════════════════════════════════════════════════

@app.post("/api/briefing")
async def daily_briefing(request: Request):
    try:
        data      = await request.json()
        user_name = data.get("user_name", "")
        agenda    = data.get("agenda", "")
        date      = data.get("date", "")
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

First person. Under 120 words total."""
        briefing = generate(system_prompt, user_message, temperature=0.8)
        return {"briefing": briefing}
    except Exception as e:
        return {"error": str(e)}


# ════════════════════════════════════════════════════════════════
# INGEST HISTORY
# ════════════════════════════════════════════════════════════════

@app.post("/api/ingest-history")
async def api_ingest_history(request: Request):
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
        from ingest.chunker import Chunker
        from ingest.embedder import CorpusStore
        lines = [
            f"TITLE: {platform.upper()} History — {user_name}",
            f"DATE: {datetime.now().isoformat()}",
            "=" * 60, "",
        ]
        for item in items[:100]:
            if platform == "gmail":
                lines += [f"[EMAIL SENT] Subject: {item.get('subject','')}", f"To: {item.get('to','')}", item.get("body",""), "─"*40, ""]
            elif platform == "slack":
                lines += [f"[SLACK] #{item.get('channel','')}", item.get("text",""), "─"*40, ""]
        tmp_path = Path(f"data/_extension_{platform}_history.txt")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text("\n".join(lines))
        user_id = data.get("user_email") or (user_name.lower().replace(" ", "_") if user_name else "default")
        chunker = Chunker()
        store   = CorpusStore(chroma_dir=Path("data/chroma_db"), user_id=user_id)
        chunks  = chunker.chunk_file(tmp_path)
        added   = store.add_chunks(chunks)
        print(f"📥 Ingested {len(items)} {platform} items → {added} chunks")
        return {"status": "ok", "chunks_added": added, "items_received": len(items)}
    except Exception as e:
        return {"error": str(e), "chunks_added": 0}


# ════════════════════════════════════════════════════════════════
# FEEDBACK
# ════════════════════════════════════════════════════════════════

@app.post("/api/feedback")
async def api_feedback(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"error": "invalid JSON"}
    try:
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


# ════════════════════════════════════════════════════════════════
# AI NEWS
# ════════════════════════════════════════════════════════════════

@app.get("/api/ai-news")
async def get_ai_news():
    try:
        import urllib.request
        import xml.etree.ElementTree as ET
        feeds = [
            ("https://hnrss.org/newest?q=AI+LLM+agent&count=5", "Hacker News"),
            ("https://www.anthropic.com/rss.xml", "Anthropic"),
        ]
        news = []
        for url, source in feeds:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    root = ET.parse(resp).getroot()
                    for item in root.findall(".//item")[:3]:
                        title = item.findtext("title", "").strip()
                        link  = item.findtext("link", "").strip()
                        pub   = item.findtext("pubDate", "").strip()
                        if title and link:
                            news.append({"title": title[:100], "url": link, "source": source, "time": pub[:16] if pub else ""})
            except Exception:
                continue
        if not news:
            news = [
                {"title": "Anthropic releases Claude 4", "url": "https://anthropic.com", "source": "Anthropic", "time": "Today"},
                {"title": "OpenAI announces GPT-5",       "url": "https://openai.com",   "source": "OpenAI",    "time": "Today"},
            ]
        return {"news": news[:8]}
    except Exception as e:
        return {"news": [], "error": str(e)}


# ════════════════════════════════════════════════════════════════
# ZOOM
# ════════════════════════════════════════════════════════════════

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
        user_message  = f"""Analyze this meeting transcript and return a JSON object with these exact keys:
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
        raw        = generate(system_prompt, user_message, temperature=0.3)
        json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        result     = _json.loads(json_match.group() if json_match else raw.strip())
        result["id"]                 = f"{user_email}_{date}_{title[:20]}".replace(" ", "_")
        result["meeting_title"]      = title
        result["meeting_date"]       = date
        result["user_email"]         = user_email
        result["transcript_preview"] = transcript[:500]
        safe_id  = _re.sub(r"[^a-zA-Z0-9_-]", "_", result["id"])[:80]
        (_ZOOM_DIR / f"{safe_id}.json").write_text(_json.dumps(result, indent=2))
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


# ════════════════════════════════════════════════════════════════
# SELF-HEALER
# ════════════════════════════════════════════════════════════════

@app.post("/api/healer/run")
async def run_healer(request: Request):
    try:
        data       = await request.json()
        auto_apply = data.get("auto_apply", False)
        from agent.self_healer import run_batch_healer
        return run_batch_healer(auto_apply=auto_apply)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/healer/feedback")
async def log_healer_feedback(request: Request):
    try:
        data = await request.json()
        from agent.self_healer import save_feedback
        save_feedback({
            "timestamp":     datetime.now().isoformat(),
            "feedback_type": data.get("feedback_type", ""),
            "platform":      data.get("platform", "email"),
            "task_type":     data.get("task_type", "draft"),
            "draft":         data.get("draft", ""),
            "edited_draft":  data.get("edited_draft", ""),
            "input_text":    data.get("input_text", ""),
            "user_email":    data.get("user_email", "default"),
        })
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/healer/report")
async def get_healer_report():
    try:
        from agent.self_healer import HEALER_REPORT
        if HEALER_REPORT.exists():
            return _json.loads(HEALER_REPORT.read_text())
        return {"error": "No report yet. Run the healer first."}
    except Exception as e:
        return {"error": str(e)}


# ════════════════════════════════════════════════════════════════
# STAKEHOLDERS
# ════════════════════════════════════════════════════════════════

@app.post("/api/stakeholders/analyze")
async def analyze_stakeholders(request: Request):
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
    try:
        from agent.stakeholder_intelligence import get_all_profiles
        return {"profiles": get_all_profiles(user_email)}
    except Exception as e:
        return {"profiles": [], "error": str(e)}


@app.post("/api/stakeholders/update")
async def update_stakeholder_profile(request: Request):
    try:
        data = await request.json()
        from agent.stakeholder_intelligence import update_profile_from_draft
        success = update_profile_from_draft(
            data.get("user_email", "default"),
            data.get("stakeholder_name", ""),
            data.get("draft", ""),
            data.get("feedback", "approved"),
            data.get("context", ""),
        )
        return {"success": success}
    except Exception as e:
        return {"error": str(e)}


# ════════════════════════════════════════════════════════════════
# INBOX ACTIVITY
# ════════════════════════════════════════════════════════════════

@app.get("/api/inbox/activity")
async def inbox_activity(user_email: str = "default"):
    try:
        from agent.self_healer import load_feedback
        feedback = load_feedback()
        user_feedback = [
            f for f in feedback
            if f.get("user_email", "default") == user_email or user_email == "default"
        ]
        gmail, outlook, teams, other = [], [], [], []
        for f in reversed(user_feedback):
            platform = f.get("platform", "email").lower()
            entry = {
                "timestamp":     f.get("timestamp", ""),
                "sender":        f.get("sender", f.get("input_text", "")[:40]),
                "subject":       f.get("subject", f.get("task_type", "Draft")),
                "draft_snippet": f.get("draft", "")[:120],
                "feedback":      f.get("feedback_type", ""),
                "platform":      platform,
            }
            if "gmail" in platform:
                gmail.append(entry)
            elif "outlook" in platform or "office" in platform:
                outlook.append(entry)
            elif "teams" in platform or "slack" in platform:
                teams.append(entry)
            else:
                other.append(entry)
        return {"gmail": gmail[:10], "outlook": outlook[:10], "teams": teams[:10], "other": other[:5], "total": len(user_feedback)}
    except Exception as e:
        return {"gmail": [], "outlook": [], "teams": [], "other": [], "total": 0, "error": str(e)}


# ════════════════════════════════════════════════════════════════
# RISK ENGINE  (duplicate routes removed — kept once each)
# ════════════════════════════════════════════════════════════════

@app.post("/api/risk/analyze")
async def analyze_communication_risk(request: Request):
    try:
        data = await request.json()
        from agent.risk_engine import analyze_risk
        return analyze_risk(
            data.get("draft", ""),
            data.get("stakeholder_name", ""),
            data.get("user_email", "default"),
            data.get("platform", "email"),
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/risk/summary")
async def get_risk_summary(user_email: str = "default", stakeholder_name: str = ""):
    try:
        from agent.risk_engine import get_stakeholder_risk_summary
        return get_stakeholder_risk_summary(user_email, stakeholder_name)
    except Exception as e:
        return {"error": str(e)}


# ════════════════════════════════════════════════════════════════
# STORAGE HELPERS
# ════════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════════
# TEXT HELPERS
# ════════════════════════════════════════════════════════════════

def _clean_html(html: str) -> str:
    if not html: return ""
    text = re.sub(r"<[^>]+>", " ", html)
    for e, c in [("&nbsp;"," "),("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"'),("&#39;","'")]:
        text = text.replace(e, c)
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


# ════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("╔════════════════════════════════════════╗")
    print("║   Alterus Webhook Server v3            ║")
    print("║   Graph API native — no Power Automate ║")
    print("╚════════════════════════════════════════╝\n")
    uvicorn.run("channels.webhook_server:app", host="0.0.0.0", port=PORT, reload=True)
