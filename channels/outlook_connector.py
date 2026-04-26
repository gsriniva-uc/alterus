"""
outlook_connector.py
Microsoft Graph OAuth flow for Alterus.
Handles: login, token exchange, inbox fetch, send email.

Endpoints:
  GET  /outlook/login          → redirect to Microsoft OAuth
  GET  /outlook/callback       → exchange code for token + auto-register subscriptions
  GET  /outlook/inbox          → fetch inbox emails (on-demand)
  POST /outlook/send           → send email
  GET  /outlook/status         → check if connected
  POST /outlook/disconnect     → revoke token + delete subscriptions

CHANGE FROM v1:
  On successful OAuth callback, we now automatically call
  register_all_subscriptions(). This replaces Power Automate entirely.
  The user connects once — Graph pushes new emails to Alterus forever.
"""

import os
import json
import httpx
from pathlib import Path
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse

# ── Encrypted token storage (item #9) ────────────────────────────────────────
from channels.token_store import save_token, load_token, delete_token

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID    = os.getenv("AZURE_CLIENT_ID", "")
CLIENT_SECRET= os.getenv("AZURE_CLIENT_SECRET", "")
TENANT_ID    = os.getenv("AZURE_TENANT_ID", "common")
REDIRECT_URI = os.getenv("OUTLOOK_REDIRECT_URI", "https://alterus.onrender.com/outlook/callback")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://app.alterus.io")

SCOPES = "openid email profile Mail.Read Mail.ReadWrite Mail.Send Calendars.Read Chat.Read offline_access"

router = APIRouter(prefix="/outlook", tags=["outlook"])


# ── Token helpers ─────────────────────────────────────────────────────────────

async def get_valid_access_token(user_email: str) -> str | None:
    """Return valid access token, refreshing if needed."""
    token = load_token(user_email)
    if not token:
        return None

    saved_at   = datetime.fromisoformat(token.get("saved_at", "2000-01-01"))
    expires_in = token.get("expires_in", 3600)
    if datetime.utcnow() > saved_at + timedelta(seconds=expires_in - 300):
        return await _refresh_token(token.get("refresh_token", ""), user_email)

    return token.get("access_token")


async def _refresh_token(refresh_token: str, user_email: str) -> str | None:
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "scope":         SCOPES,
        })
        if resp.status_code == 200:
            data = resp.json()
            data["user_email"] = user_email
            save_token(user_email, data)
            return data.get("access_token")
    return None


# ── OAuth Endpoints ───────────────────────────────────────────────────────────

@router.get("/login")
async def outlook_login(user_email: str = "default"):
    if not CLIENT_ID:
        return JSONResponse({"error": "AZURE_CLIENT_ID not configured"}, status_code=500)

    auth_url = (
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPES.replace(' ', '%20')}"
        f"&response_mode=query"
        f"&state={user_email}"
        f"&prompt=select_account"
    )
    return RedirectResponse(auth_url)


@router.get("/callback")
async def outlook_callback(
    background_tasks: BackgroundTasks,
    code: str = "",
    state: str = "default",
    error: str = "",
):
    """
    Handle OAuth callback from Microsoft.

    After saving the token, automatically registers Graph subscriptions
    in the background so the user never has to configure anything else.
    """
    if error:
        return RedirectResponse(f"{FRONTEND_URL}/dashboard?outlook_error={error}")
    if not code:
        return RedirectResponse(f"{FRONTEND_URL}/dashboard?outlook_error=no_code")

    user_email = state

    # Exchange code for token
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
            "grant_type":    "authorization_code",
            "scope":         SCOPES,
        })
        if resp.status_code != 200:
            return RedirectResponse(f"{FRONTEND_URL}/dashboard?outlook_error=token_exchange_failed")
        token_data = resp.json()

    access_token = token_data.get("access_token")

    # Resolve actual email if state was "default"
    if user_email == "default" and access_token:
        async with httpx.AsyncClient() as client:
            me = await client.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if me.status_code == 200:
                user_email = (
                    me.json().get("mail")
                    or me.json().get("userPrincipalName", "default")
                )

    token_data["user_email"] = user_email
    save_token(user_email, token_data)

    # ── Auto-register Graph subscriptions ────────────────────────────────────
    # Runs in background so the OAuth redirect responds immediately.
    # This is what replaces Power Automate — Graph will now push new emails
    # and calendar events directly to our webhook endpoints.
    background_tasks.add_task(
        _register_subscriptions_background, user_email, access_token
    )

    print(f"✅ Outlook connected for {user_email} — registering Graph subscriptions...")
    return RedirectResponse(
        f"{FRONTEND_URL}/dashboard?outlook_connected=true&user={user_email}"
    )


async def _register_subscriptions_background(user_email: str, access_token: str):
    """Background task called after OAuth — user never waits for this."""
    try:
        from channels.graph_subscriptions import register_all_subscriptions
        results = await register_all_subscriptions(user_email, access_token)
        email_ok = results.get("email", {}).get("success", False)
        cal_ok   = results.get("calendar", {}).get("success", False)
        print(
            f"📡 Graph subscriptions for {user_email}: "
            f"email={'✅' if email_ok else '❌'} "
            f"calendar={'✅' if cal_ok else '❌'}"
        )
    except Exception as e:
        print(f"⚠️  Subscription registration failed for {user_email}: {e}")


# ── Status & disconnect ───────────────────────────────────────────────────────

@router.get("/status")
async def outlook_status(user_email: str = "default"):
    token = load_token(user_email)
    if not token:
        return {"connected": False}

    # Also report subscription status
    from channels.graph_subscriptions import get_subscriptions_for_user
    subs = get_subscriptions_for_user(user_email)

    return {
        "connected":     True,
        "email":         token.get("user_email", user_email),
        "subscriptions": [
            {"label": s["label"], "expires": s["expiry_dt"]}
            for s in subs
        ],
        "realtime_active": len(subs) > 0,
    }


@router.post("/disconnect")
async def outlook_disconnect(request: Request):
    """Disconnect Outlook — deletes token AND cancels Graph subscriptions."""
    data       = await request.json()
    user_email = data.get("user_email", "default")

    # Delete subscriptions first (needs valid token)
    access_token = await get_valid_access_token(user_email)
    if access_token:
        from channels.graph_subscriptions import delete_subscriptions_for_user
        await delete_subscriptions_for_user(user_email, access_token)

    delete_token(user_email)
    return {"success": True}


# ── On-demand inbox fetch (still useful for initial load + manual refresh) ────

@router.get("/inbox")
async def outlook_inbox(user_email: str = "default", top: int = 20):
    """
    Fetch inbox emails on-demand from Graph.
    Used for: initial dashboard load, manual refresh button.
    Real-time new emails arrive via Graph push notifications to /webhook/email.
    """
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected", "emails": []}, status_code=401)

    url = (
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
        f"?$top={top}"
        "&$orderby=receivedDateTime desc"
        "&$select=id,subject,from,receivedDateTime,bodyPreview,isRead,conversationId,importance"
    )

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    if resp.status_code == 401:
        delete_token(user_email)
        return JSONResponse({"error": "token_expired", "emails": []}, status_code=401)
    if resp.status_code != 200:
        return JSONResponse({"error": "graph_error", "emails": []}, status_code=500)

    emails = []
    for msg in resp.json().get("value", []):
        sender = msg.get("from", {}).get("emailAddress", {})
        emails.append({
            "id":             msg.get("id"),
            "subject":        msg.get("subject", "(no subject)"),
            "from":           sender.get("name", sender.get("address", "Unknown")),
            "from_email":     sender.get("address", ""),
            "preview":        msg.get("bodyPreview", "")[:150],
            "time":           _format_time(msg.get("receivedDateTime", "")),
            "receivedAt":     msg.get("receivedDateTime", ""),
            "unread":         not msg.get("isRead", True),
            "conversationId": msg.get("conversationId", ""),
            "importance":     msg.get("importance", "normal"),
            "source":         "outlook_graph",
        })

    return {"emails": emails, "count": len(emails)}


@router.get("/email/{message_id}")
async def get_email_body(message_id: str, user_email: str = "default"):
    """Fetch full email body by message ID."""
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    url = (
        f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
        "?$select=id,subject,from,body,receivedDateTime,toRecipients,ccRecipients"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    if resp.status_code != 200:
        return JSONResponse({"error": "not_found"}, status_code=404)

    msg    = resp.json()
    sender = msg.get("from", {}).get("emailAddress", {})
    body   = msg.get("body", {})
    return {
        "id":           msg.get("id"),
        "subject":      msg.get("subject", ""),
        "from":         sender.get("name", ""),
        "from_email":   sender.get("address", ""),
        "body":         body.get("content", ""),
        "content_type": body.get("contentType", "text"),
        "to":           [r.get("emailAddress", {}).get("address") for r in msg.get("toRecipients", [])],
        "cc":           [r.get("emailAddress", {}).get("address") for r in msg.get("ccRecipients", [])],
        "receivedAt":   msg.get("receivedDateTime", ""),
    }


@router.post("/send")
async def send_email(request: Request):
    """Send email via Microsoft Graph."""
    data         = await request.json()
    user_email   = data.get("user_email", "default")
    to           = data.get("to", "")
    subject      = data.get("subject", "")
    body         = data.get("body", "")
    cc           = data.get("cc", "")

    if not all([to, subject, body]):
        return JSONResponse({"error": "Missing to, subject, or body"}, status_code=400)

    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": a.strip()}} for a in to.split(",") if a.strip()],
    }
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": a.strip()}} for a in cc.split(",") if a.strip()]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"message": message, "saveToSentItems": True},
        )

    if resp.status_code == 202:
        print(f"📤 Email sent to {to} by {user_email}")
        return {"success": True}
    return JSONResponse({"error": "send_failed", "detail": resp.text}, status_code=500)


@router.post("/mark-read/{message_id}")
async def mark_read(message_id: str, request: Request):
    data         = await request.json()
    user_email   = data.get("user_email", "default")
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"https://graph.microsoft.com/v1.0/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"isRead": True},
        )
    return {"success": resp.status_code == 200}


# ── Teams on-demand poll endpoint ─────────────────────────────────────────────

@router.get("/teams")
async def get_teams_messages(user_email: str = "default", limit: int = 20):
    """
    Fetch recent Teams messages on-demand (polling).
    Real-time Teams push notifications require admin-consented app permissions.
    This is the v1 approach — works with per-user delegated OAuth.
    """
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected", "messages": []}, status_code=401)

    from channels.graph_subscriptions import poll_teams_messages
    messages = await poll_teams_messages(user_email, access_token, limit)
    return {"messages": messages, "count": len(messages), "source": "teams_graph_poll"}


# ── Calendar on-demand fetch ──────────────────────────────────────────────────

@router.get("/calendar")
async def get_calendar(user_email: str = "default", days_ahead: int = 7):
    """Fetch upcoming calendar events on-demand."""
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected", "events": []}, status_code=401)

    from datetime import timezone
    now   = datetime.now(timezone.utc)
    end   = now + timedelta(days=days_ahead)
    start = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_s = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (
        f"https://graph.microsoft.com/v1.0/me/calendarView"
        f"?startDateTime={start}&endDateTime={end_s}"
        f"&$top=20&$orderby=start/dateTime"
        f"&$select=id,subject,start,end,location,organizer,attendees,isOnlineMeeting,onlineMeetingUrl"
    )

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    if resp.status_code != 200:
        return JSONResponse({"error": "calendar_error", "events": []}, status_code=500)

    events = []
    for e in resp.json().get("value", []):
        events.append({
            "id":          e.get("id"),
            "title":       e.get("subject", "(no title)"),
            "start":       e.get("start", {}).get("dateTime", ""),
            "end":         e.get("end", {}).get("dateTime", ""),
            "location":    e.get("location", {}).get("displayName", ""),
            "organizer":   e.get("organizer", {}).get("emailAddress", {}).get("name", ""),
            "isOnlineMtg": e.get("isOnlineMeeting", False),
            "joinUrl":     e.get("onlineMeetingUrl", ""),
            "attendees":   [
                a.get("emailAddress", {}).get("name", "")
                for a in e.get("attendees", [])
            ],
            "source": "calendar_graph",
        })

    return {"events": events, "count": len(events)}


# ── Helper ────────────────────────────────────────────────────────────────────

def _format_time(iso_str: str) -> str:
    if not iso_str:
        return "Just now"
    try:
        dt   = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now  = datetime.now(dt.tzinfo)
        diff = now - dt
        if diff.days == 0:   return dt.strftime("%-I:%M %p")
        elif diff.days == 1: return "Yesterday"
        else:                return dt.strftime("%b %d")
    except Exception:
        return "Just now"
