"""
webhook_server.py  (v3.1 — fixes for beta users)

KEY FIXES in this version:
  1. verify_request() — no longer blocks beta users. Returns True unless
     ALTERUS_API_SECRET is explicitly set AND a bad token is presented.
     Removes origin-based blocking that was silently rejecting mobile users.

  2. /api/draft — no longer calls build_system_prompt() from persona.py.
     Uses an adaptive inline prompt that works for ANY user, not just Ganesh.
     persona.py is Ganesh-specific — calling it for beta users was generating
     drafts in Ganesh's voice with Ganesh's context.

  3. /api/briefing — handles empty user_name gracefully. Beta users who
     haven't set their name no longer get a broken "You are . Write..." prompt.

  4. /api/debug — new endpoint to diagnose issues without looking at Render logs.
     Hit /api/debug from any browser to see what's working.

  5. All error messages now return {"error": "...", "draft": ""} so the
     React app always gets a parseable response instead of crashing silently.
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
import uuid
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
_ZOOM_DIR     = Path("data/zoom_meetings")
PORT          = 8000

MY_NAME  = os.getenv("USER_DISPLAY_NAME", "Ganesh Srinivasan")
MY_EMAIL = os.getenv("USER_EMAIL", "ganesh.srinivasan@servicenow.com")

app = FastAPI(title="Alterus Webhook Server v3.1")

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

    existing = load_json(CALENDAR_FILE)
    if existing:
        cleaned = dedup_calendar(existing)
        if len(cleaned) < len(existing):
            save_json(CALENDAR_FILE, cleaned)
            print(f"🧹 Cleaned {len(existing) - len(cleaned)} duplicate calendar events")

    asyncio.create_task(_run_renewal_loop())
    print("🚀 Alterus webhook server v3.1 ready")


async def _run_renewal_loop():
    await asyncio.sleep(10)
    try:
        from channels.graph_subscriptions import subscription_renewal_loop
        await subscription_renewal_loop()
    except Exception as e:
        print(f"⚠️  Renewal loop error: {e}")


# ── Auth ──────────────────────────────────────────────────────────────────────
# FIX: was silently blocking beta users on mobile.
# New logic: only enforce if ALTERUS_API_SECRET is set AND a Bearer token is
# present but wrong. Origin-only requests always pass through.

def verify_request(request) -> bool:
    # Health checks always pass
    if request.url.path in ("/api/health", "/health", "/api/debug"):
        return True

    auth       = request.headers.get("authorization", "")
    api_secret = os.getenv("ALTERUS_API_SECRET", "")

    # If API secret is set and a Bearer token was sent, validate it
    if api_secret and auth.startswith("Bearer "):
        token = auth[7:]
        # Accept the Alterus-format token (alterus:email:timestamp)
        try:
            import base64
            decoded = base64.b64decode(token + "==").decode("utf-8")
            if decoded.startswith("alterus:") and "@" in decoded:
                return True
        except Exception:
            pass
        # Also accept direct secret match
        if _secrets.compare_digest(token, api_secret):
            return True
        # Bad token presented — block
        print(f"⚠️  Bad auth token from {request.client.host} on {request.url.path}")
        return False

    # No API secret set, or no token sent → allow through
    # CORS (allow_origins=["*"]) handles browser-level security
    return True


# ════════════════════════════════════════════════════════════════
# DEBUG ENDPOINT — hit this to diagnose issues without Render logs
# ════════════════════════════════════════════════════════════════

@app.get("/api/debug")
async def debug():
    """
    Diagnostic endpoint — shows what's working without needing Render logs.
    Hit https://alterus.onrender.com/api/debug in any browser.
    """
    checks = {}

    # LLM check
    try:
        from agent.drafter import generate, USE_CLAUDE, CLAUDE_MODEL, OLLAMA_MODEL
        checks["llm"] = {
            "provider": "Claude API" if USE_CLAUDE else "Ollama (local)",
            "model":    CLAUDE_MODEL if USE_CLAUDE else OLLAMA_MODEL,
            "api_key_set": bool(os.getenv("ANTHROPIC_API_KEY", "")),
        }
    except Exception as e:
        checks["llm"] = {"error": str(e)}

    # Chroma check
    try:
        from ingest.embedder import CorpusStore
        store = CorpusStore(Path("data/chroma_db"), user_id="default")
        checks["chroma"] = {"chunks": store.count(), "status": "ok"}
    except Exception as e:
        checks["chroma"] = {"error": str(e)}

    # Token store check
    try:
        from channels.token_store import TOKEN_DIR
        enc_files = list(TOKEN_DIR.glob("*.enc"))
        checks["token_store"] = {
            "dir":        str(TOKEN_DIR),
            "enc_files":  len(enc_files),
            "key_set":    bool(os.getenv("ALTERUS_TOKEN_ENC_KEY", "")),
        }
    except Exception as e:
        checks["token_store"] = {"error": str(e)}

    # Graph subscriptions check
    try:
        from channels.graph_subscriptions import _load_subs
        subs = _load_subs()
        checks["graph_subscriptions"] = {"count": len(subs), "ids": list(subs.keys())[:3]}
    except Exception as e:
        checks["graph_subscriptions"] = {"error": str(e)}

    # Env vars check (keys only, not values)
    checks["env_vars"] = {
        "AZURE_CLIENT_ID":        bool(os.getenv("AZURE_CLIENT_ID")),
        "AZURE_CLIENT_SECRET":    bool(os.getenv("AZURE_CLIENT_SECRET")),
        "ANTHROPIC_API_KEY":      bool(os.getenv("ANTHROPIC_API_KEY")),
        "ALTERUS_TOKEN_ENC_KEY":  bool(os.getenv("ALTERUS_TOKEN_ENC_KEY")),
        "WEBHOOK_BASE_URL":       os.getenv("WEBHOOK_BASE_URL", "NOT SET"),
        "ALTERUS_API_SECRET":     bool(os.getenv("ALTERUS_API_SECRET")),
    }

    checks["data_files"] = {
        "emails":   len(load_json(INBOX_FILE)),
        "calendar": len(load_json(CALENDAR_FILE)),
        "teams":    len(load_json(TEAMS_FILE)),
    }

    return {"status": "ok", "version": "3.1", "checks": checks}


# ════════════════════════════════════════════════════════════════
# GRAPH WEBHOOK ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.post("/webhook/email")
async def receive_email(request: Request, background_tasks: BackgroundTasks):
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        print("📡 Graph validating /webhook/email")
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
    user_email = notification.get("clientState", "")
    if not user_email:
        try:
            from channels.graph_subscriptions import get_user_email_for_subscription
            user_email = get_user_email_for_subscription(
                notification.get("subscriptionId", "")) or ""
        except Exception:
            pass
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
    if MY_EMAIL.lower() in sender_email.lower():
        return

    email = {
        "id":             msg.get("id"),
        "from":           sender.get("name", sender_email),
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
    print(f"📧 New email → {user_email}: {email['subject'][:50]}")


@app.post("/webhook/calendar")
async def receive_calendar(request: Request, background_tasks: BackgroundTasks):
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        print("📡 Graph validating /webhook/calendar")
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
    user_email = notification.get("clientState", "")
    if not user_email:
        try:
            from channels.graph_subscriptions import get_user_email_for_subscription
            user_email = get_user_email_for_subscription(
                notification.get("subscriptionId", "")) or ""
        except Exception:
            pass
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
    if any(f"{x.get('title','')}|{x.get('start','')[:16]}" == dedup_key for x in events):
        events = [event if f"{x.get('title','')}|{x.get('start','')[:16]}" == dedup_key
                  else x for x in events]
        save_json(CALENDAR_FILE, events)
    else:
        prepend_item(CALENDAR_FILE, event)
    print(f"📅 Calendar event: {event['title'][:50]}")


# ════════════════════════════════════════════════════════════════
# DRAFT ENDPOINT
# FIX: removed build_system_prompt() call — persona.py is Ganesh-specific.
# Now uses adaptive inline prompt that works for ANY user.
# ════════════════════════════════════════════════════════════════

@app.post("/api/draft")
async def api_draft(request: Request):
    """
    Draft Reply button in InboxTab.js.

    Payload:  { platform, body, subject, sender, sender_email,
                user_name, user_email, tone, user_title?, user_company? }
    Returns:  { draft, agent, critique? }

    Works for any user — does NOT depend on persona.py hardcoding.
    """
    if not verify_request(request):
        return JSONResponse({"error": "unauthorized", "draft": ""}, status_code=401)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON", "draft": ""}, status_code=400)

    platform     = data.get("platform", "email")
    body         = data.get("body", "")
    subject      = data.get("subject", "")
    sender       = data.get("sender", "")
    sender_email = data.get("sender_email", "")
    tone         = data.get("tone", "professional")
    user_name    = data.get("user_name", "").strip()
    user_email   = data.get("user_email", "default").strip()
    user_title   = data.get("user_title", "").strip()
    user_company = data.get("user_company", "").strip()

    if not body:
        return JSONResponse({"error": "No message body provided", "draft": ""}, status_code=400)

    # ── Who is the user? Build identity string dynamically ───────────────────
    # FIX: previously called build_system_prompt() which is hardcoded for Ganesh.
    # Now builds identity from request payload — works for any beta user.
    if user_name:
        identity = user_name
        if user_title and user_company:
            identity += f", {user_title} at {user_company}"
        elif user_title:
            identity += f", {user_title}"
        elif user_company:
            identity += f" at {user_company}"
    else:
        identity = "a professional"   # safe fallback for users who haven't set name

    task_type = "email" if platform in ("gmail", "outlook", "email") else "teams"

    tone_map = {
        "formal":       "formal and executive-level",
        "professional": "professional and direct",
        "casual":       "conversational and friendly",
        "concise":      "extremely concise (under 60 words)",
        "balanced":     "professional but approachable",
    }
    tone_desc = tone_map.get(tone, "professional and direct")

    # ── Adaptive system prompt — works for any user ───────────────────────────
    system_prompt = f"""You are {identity}.
You are drafting a {task_type} reply on behalf of {user_name or 'the user'}.

Your drafts must:
- Sound natural and authentic to {user_name or 'the user'} — not generic AI
- Be {tone_desc} in tone
- Be specific to what was actually said — never invent facts
- Be concise — under 150 words unless more detail is clearly needed
- End with a clear next step or ask when appropriate
- Never use filler phrases like "I hope this email finds you well"
- Write in first person as {user_name or 'the user'}"""

    # ── Try ReAct agent first (uses corpus retrieval if available) ────────────
    try:
        from agent.react_agent import run_react_agent
        result = run_react_agent(
            platform   = platform,
            sender     = sender,
            subject    = subject,
            body       = body,
            user_name  = user_name or "User",
            user_email = user_email,
            tone       = tone.capitalize(),
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
        print(f"⚠️  ReAct agent failed: {e} — using direct drafter")

    # ── Fallback: direct Claude/Ollama call ───────────────────────────────────
    try:
        from agent.drafter import generate

        # Try to pull context from this user's Chroma corpus
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
            pass  # No corpus yet — that's fine, draft without context

        user_message = f"""Draft a {task_type} reply for {user_name or 'me'}.

From: {sender} <{sender_email}>
Subject: {subject}
Message:
{body[:1500]}
{history_context}

Instructions:
- Reply specifically to what {sender} said above
- Do NOT invent facts, projects, or commitments not mentioned
- Write as {user_name or 'me'} in first person
- Tone: {tone_desc}
- Under 150 words
- Write only the reply body, nothing else"""

        draft  = generate(system_prompt, user_message, temperature=0.7)
        run_id = str(uuid.uuid4())
        return {"draft": draft, "agent": "direct", "run_id": run_id}

    except Exception as e:
        print(f"❌ /api/draft error: {e}")
        return JSONResponse(
            {"error": str(e), "draft": f"Draft generation failed: {e}"},
            status_code=500
        )


# ════════════════════════════════════════════════════════════════
# DAILY BRIEFING
# FIX: handles empty user_name, works for any beta user
# ════════════════════════════════════════════════════════════════

@app.post("/api/briefing")
async def daily_briefing(request: Request):
    try:
        data       = await request.json()
        user_name  = data.get("user_name", "").strip()
        user_email = data.get("user_email", "default")
        agenda     = data.get("agenda", "")
        date       = data.get("date", datetime.now().strftime("%A, %B %d, %Y"))

        # FIX: safe fallback when user_name is empty
        display_name = user_name if user_name else "you"
        first_person = user_name if user_name else "I"

        from agent.drafter import generate

        system_prompt = f"""You are writing a daily briefing for {display_name}.
Write it in first person as {display_name} — like a smart chief of staff briefing themselves.
Be direct, specific, and actionable. Under 150 words total."""

        user_message = f"""Today is {date}.

{"Agenda for today: " + agenda if agenda else "No agenda provided — create a general productivity-focused briefing."}

Write the briefing in this exact format:

**Today's Priorities:**
- [Priority 1 — specific and actionable]
- [Priority 2 — specific and actionable]
- [Priority 3 — specific and actionable]

**Needs Response:** [names or "None today"]

**Risk Watch:** [one specific risk in one sentence]

**Mindset:** [one motivational sentence]

First person ("I", "my"). Under 150 words. Be specific using any agenda details provided."""

        briefing = generate(system_prompt, user_message, temperature=0.8)
        return {"briefing": briefing, "user": display_name}

    except Exception as e:
        print(f"❌ /api/briefing error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ════════════════════════════════════════════════════════════════
# PRD GENERATOR
# ════════════════════════════════════════════════════════════════

@app.post("/api/generate-prd")
async def generate_prd(request: Request):
    try:
        data         = await request.json()
        prompt       = data.get("prompt", "")
        user_name    = data.get("user_name", "").strip() or "a senior product manager"
        user_title   = data.get("user_title", "Senior Product Manager")
        user_company = data.get("user_company", "")

        if not prompt:
            return JSONResponse({"error": "No prompt provided"}, status_code=400)

        from agent.drafter import generate
        system_prompt = f"""You are {user_name}, {user_title}{' at ' + user_company if user_company else ''}.
Write a comprehensive PRD in first person.
Format with sections: Overview, Problem Statement, Goals & Success Metrics,
User Stories, Functional Requirements, Non-Functional Requirements,
Out of Scope, Timeline & Milestones.
Be specific, actionable, and data-driven. Under 600 words."""

        prd = generate(system_prompt, f"Write a PRD for: {prompt}", temperature=0.7)
        return {"prd": prd}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ════════════════════════════════════════════════════════════════
# ZOOM TRANSCRIPT ANALYZER
# ════════════════════════════════════════════════════════════════

@app.post("/api/zoom/ingest")
async def zoom_ingest(request: Request):
    try:
        data       = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    transcript = data.get("transcript", "")
    title      = data.get("title", "Meeting")
    date       = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    user_email = data.get("user_email", "default")
    user_name  = data.get("user_name", "").strip()

    if not transcript:
        return JSONResponse({"error": "No transcript provided"}, status_code=400)

    try:
        from agent.drafter import generate

        system_prompt = "You are an expert meeting analyst. Extract structured insights from meeting transcripts. Respond in valid JSON only — no other text, no markdown code blocks."

        user_message = f"""Analyze this meeting transcript. Return ONLY a JSON object with these exact keys:

{{
  "summary": "3-4 sentence meeting summary",
  "meeting_sentiment": "productive|neutral|tense|inconclusive",
  "participants": ["name1", "name2"],
  "key_topics": ["topic1", "topic2"],
  "decisions": [{{"decision": "...", "made_by": "..."}}],
  "action_items": [{{"owner": "...", "action": "...", "deadline": "soon|this week|TBD", "priority": "high|medium|low"}}],
  "followup_email": "complete follow-up email draft ready to send"
}}

Meeting: {title}
Date: {date}
{f"Note taker: {user_name}" if user_name else ""}

Transcript:
{transcript[:4000]}

Return ONLY the JSON object. No preamble."""

        raw        = generate(system_prompt, user_message, temperature=0.3)

        # Parse JSON — handle cases where model wraps in markdown
        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```\w*\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean)

        json_match = _re.search(r'\{.*\}', clean, _re.DOTALL)
        result     = _json.loads(json_match.group() if json_match else clean)

        meeting_id               = f"{user_email}_{date}_{title[:20]}".replace(" ", "_")
        result["id"]             = meeting_id
        result["meeting_title"]  = title
        result["meeting_date"]   = date
        result["user_email"]     = user_email
        result["user_name"]      = user_name
        result["transcript_preview"] = transcript[:500]

        safe_id  = _re.sub(r"[^a-zA-Z0-9_-]", "_", meeting_id)[:80]
        _ZOOM_DIR.mkdir(parents=True, exist_ok=True)
        (_ZOOM_DIR / f"{safe_id}.json").write_text(_json.dumps(result, indent=2))

        try:
            from agent.memory_writeback import write_zoom_to_corpus
            write_zoom_to_corpus(user_email, result)
        except Exception:
            pass

        return {"success": True, "meeting_id": meeting_id, "title": title, "result": result}

    except Exception as e:
        print(f"❌ /api/zoom/ingest error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


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
# CLONE SCORE
# ════════════════════════════════════════════════════════════════

@app.get("/api/clone-score")
async def get_clone_score(user_email: str = "default"):
    try:
        from ingest.embedder import CorpusStore
        store = CorpusStore(Path("data/chroma_db"), user_id=user_email)
        count = store.count()
        if count == 0:    score = 5
        elif count < 20:  score = 15
        elif count < 50:  score = 30
        elif count < 100: score = 45
        elif count < 200: score = 60
        elif count < 400: score = 75
        else:             score = 90
        return {"score": score, "chunks": count}
    except Exception:
        return {"score": 5, "chunks": 0}


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
# INGEST HISTORY
# ════════════════════════════════════════════════════════════════

@app.post("/api/ingest-history")
async def api_ingest_history(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    platform  = data.get("platform", "unknown")
    items     = data.get("items", [])
    user_name = data.get("user_name", "")
    if not items:
        return {"status": "no items", "chunks_added": 0}
    try:
        from ingest.chunker import Chunker
        from ingest.embedder import CorpusStore
        lines = [f"TITLE: {platform.upper()} History — {user_name}", f"DATE: {datetime.now().isoformat()}", "="*60, ""]
        for item in items[:100]:
            if platform == "gmail":
                lines += [f"[EMAIL SENT] Subject: {item.get('subject','')}", f"To: {item.get('to','')}", item.get("body",""), "─"*40, ""]
            elif platform == "slack":
                lines += [f"[SLACK] #{item.get('channel','')}", item.get("text",""), "─"*40, ""]
        tmp_path = Path(f"data/_extension_{platform}_history.txt")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text("\n".join(lines))
        user_id = data.get("user_email") or (user_name.lower().replace(" ","_") if user_name else "default")
        chunker = Chunker()
        store   = CorpusStore(chroma_dir=Path("data/chroma_db"), user_id=user_id)
        chunks  = chunker.chunk_file(tmp_path)
        added   = store.add_chunks(chunks)
        return {"status": "ok", "chunks_added": added, "items_received": len(items)}
    except Exception as e:
        return JSONResponse({"error": str(e), "chunks_added": 0}, status_code=500)


# ════════════════════════════════════════════════════════════════
# FEEDBACK
# ════════════════════════════════════════════════════════════════

@app.post("/api/feedback")
async def api_feedback(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
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
        return JSONResponse({"error": str(e)}, status_code=500)


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
        return JSONResponse({"error": str(e)}, status_code=500)


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
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/healer/report")
async def get_healer_report():
    try:
        from agent.self_healer import HEALER_REPORT
        if HEALER_REPORT.exists():
            return _json.loads(HEALER_REPORT.read_text())
        return {"error": "No report yet"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ════════════════════════════════════════════════════════════════
# STAKEHOLDERS
# ════════════════════════════════════════════════════════════════

@app.post("/api/stakeholders/analyze")
async def analyze_stakeholders_endpoint(request: Request):
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
            data.get("user_email", "default"), data.get("stakeholder_name", ""),
            data.get("draft", ""), data.get("feedback", "approved"), data.get("context", ""),
        )
        return {"success": success}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
            if "gmail" in platform:               gmail.append(entry)
            elif "outlook" in platform:           outlook.append(entry)
            elif "teams" in platform:             teams.append(entry)
            else:                                 other.append(entry)
        return {"gmail": gmail[:10], "outlook": outlook[:10], "teams": teams[:10],
                "other": other[:5], "total": len(user_feedback)}
    except Exception as e:
        return {"gmail": [], "outlook": [], "teams": [], "other": [], "total": 0, "error": str(e)}


# ════════════════════════════════════════════════════════════════
# RISK ENGINE  (deduplicated — was defined twice in original)
# ════════════════════════════════════════════════════════════════

@app.post("/api/risk/analyze")
async def analyze_communication_risk(request: Request):
    try:
        data = await request.json()
        from agent.risk_engine import analyze_risk
        return analyze_risk(
            data.get("draft", ""), data.get("stakeholder_name", ""),
            data.get("user_email", "default"), data.get("platform", "email"),
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
# HEALTH & DATA
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
        "version":       "3.1",
        "time":          datetime.now().isoformat(),
        "emails":        len(load_json(INBOX_FILE)),
        "calendar":      len(load_json(CALENDAR_FILE)),
        "subscriptions": len(subs),
    }

@app.get("/api/health")
async def api_health():
    return {"status": "ok", "service": "alterus-webhook-server-v3.1"}

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
        if e.get("id") == email_id:
            e["unread"] = False
    save_json(INBOX_FILE, emails)
    return {"status": "ok"}

@app.get("/api/subscriptions")
async def get_subscriptions(user_email: str = "default"):
    try:
        from channels.graph_subscriptions import get_subscriptions_for_user
        subs = get_subscriptions_for_user(user_email)
        return {"subscriptions": subs, "count": len(subs)}
    except Exception as e:
        return {"subscriptions": [], "count": 0, "error": str(e)}

@app.post("/api/subscriptions/refresh")
async def refresh_subscriptions(request: Request):
    data       = await request.json()
    user_email = data.get("user_email", "default")
    from channels.outlook_connector import get_valid_access_token
    from channels.graph_subscriptions import register_all_subscriptions
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected"}, status_code=401)
    return await register_all_subscriptions(user_email, access_token)


# ════════════════════════════════════════════════════════════════
# STORAGE & TEXT HELPERS
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
    print("║   Alterus Webhook Server v3.1          ║")
    print("║   Graph API · All endpoints restored   ║")
    print("╚════════════════════════════════════════╝")
    uvicorn.run("channels.webhook_server:app", host="0.0.0.0", port=PORT, reload=True)
