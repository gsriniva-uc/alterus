"""
outlook_connector.py
Microsoft Graph OAuth flow for Alterus.
Handles: login, token exchange, inbox fetch, send email.

Endpoints:
  GET  /outlook/login          → redirect to Microsoft OAuth
  GET  /outlook/callback       → exchange code for token
  GET  /outlook/inbox          → fetch inbox emails
  POST /outlook/send           → send email
  GET  /outlook/status         → check if connected
  POST /outlook/disconnect     → revoke token
"""

import os
import json
import httpx
from pathlib import Path
from datetime import datetime, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse

# ── Config ────────────────────────────────────────────────────────────────────

CLIENT_ID     = os.getenv("AZURE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
TENANT_ID     = os.getenv("AZURE_TENANT_ID", "common")
REDIRECT_URI  = os.getenv("OUTLOOK_REDIRECT_URI", "https://alterus.onrender.com/outlook/callback")
FRONTEND_URL  = os.getenv("FRONTEND_URL", "https://app.alterus.io")

SCOPES = "openid email profile Mail.Read Mail.ReadWrite Mail.Send offline_access"

TOKEN_DIR = Path("data/outlook_tokens")
TOKEN_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="/outlook", tags=["outlook"])


# ── Token Storage ─────────────────────────────────────────────────────────────

def _token_path(user_email: str) -> Path:
    safe = user_email.replace("@", "_at_").replace(".", "_")
    return TOKEN_DIR / f"{safe}.json"


def save_token(user_email: str, token_data: dict):
    token_data["saved_at"] = datetime.utcnow().isoformat()
    _token_path(user_email).write_text(json.dumps(token_data, indent=2))


def load_token(user_email: str) -> dict | None:
    path = _token_path(user_email)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def delete_token(user_email: str):
    path = _token_path(user_email)
    if path.exists():
        path.unlink()


async def get_valid_access_token(user_email: str) -> str | None:
    """Return a valid access token, refreshing if needed."""
    token = load_token(user_email)
    if not token:
        return None

    # Check if expired (with 5 min buffer)
    saved_at  = datetime.fromisoformat(token.get("saved_at", "2000-01-01"))
    expires_in = token.get("expires_in", 3600)
    if datetime.utcnow() > saved_at + timedelta(seconds=expires_in - 300):
        # Refresh token
        refreshed = await _refresh_token(token.get("refresh_token", ""), user_email)
        if refreshed:
            return refreshed
        return None

    return token.get("access_token")


async def _refresh_token(refresh_token: str, user_email: str) -> str | None:
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    payload = {
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "scope":         SCOPES,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=payload)
        if resp.status_code == 200:
            token_data = resp.json()
            token_data["user_email"] = user_email
            save_token(user_email, token_data)
            return token_data.get("access_token")
    return None


# ── OAuth Endpoints ───────────────────────────────────────────────────────────

@router.get("/login")
async def outlook_login(user_email: str = "default"):
    """Redirect user to Microsoft OAuth login page."""
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
async def outlook_callback(code: str = "", state: str = "default", error: str = ""):
    """Handle OAuth callback from Microsoft."""
    if error:
        return RedirectResponse(f"{FRONTEND_URL}/dashboard?outlook_error={error}")

    if not code:
        return RedirectResponse(f"{FRONTEND_URL}/dashboard?outlook_error=no_code")

    user_email = state  # we passed user_email as state

    # Exchange code for token
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    payload = {
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
        "scope":         SCOPES,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=payload)
        if resp.status_code != 200:
            return RedirectResponse(f"{FRONTEND_URL}/dashboard?outlook_error=token_exchange_failed")

        token_data = resp.json()

    # Get user's actual email from Microsoft if state was "default"
    access_token = token_data.get("access_token")
    if user_email == "default" and access_token:
        async with httpx.AsyncClient() as client:
            me_resp = await client.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if me_resp.status_code == 200:
                user_email = me_resp.json().get("mail") or me_resp.json().get("userPrincipalName", "default")

    token_data["user_email"] = user_email
    save_token(user_email, token_data)

    print(f"✅ Outlook connected for {user_email}")
    return RedirectResponse(f"{FRONTEND_URL}/dashboard?outlook_connected=true&user={user_email}")


# ── Inbox & Email Endpoints ───────────────────────────────────────────────────

@router.get("/status")
async def outlook_status(user_email: str = "default"):
    """Check if Outlook is connected for this user."""
    token = load_token(user_email)
    if not token:
        return {"connected": False}
    return {
        "connected": True,
        "email": token.get("user_email", user_email),
    }


@router.post("/disconnect")
async def outlook_disconnect(request: Request):
    """Disconnect Outlook for this user."""
    data       = await request.json()
    user_email = data.get("user_email", "default")
    delete_token(user_email)
    return {"success": True}


@router.get("/inbox")
async def outlook_inbox(user_email: str = "default", top: int = 20):
    """Fetch inbox emails from Microsoft Graph."""
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected", "emails": []}, status_code=401)

    url = (
        f"https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
        f"?$top={top}"
        f"&$orderby=receivedDateTime desc"
        f"&$select=id,subject,from,receivedDateTime,bodyPreview,isRead,conversationId,importance"
    )

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    if resp.status_code == 401:
        delete_token(user_email)
        return JSONResponse({"error": "token_expired", "emails": []}, status_code=401)

    if resp.status_code != 200:
        return JSONResponse({"error": "graph_error", "emails": []}, status_code=500)

    messages = resp.json().get("value", [])

    emails = []
    for msg in messages:
        sender      = msg.get("from", {}).get("emailAddress", {})
        received_at = msg.get("receivedDateTime", "")
        emails.append({
            "id":             msg.get("id"),
            "subject":        msg.get("subject", "(no subject)"),
            "from":           sender.get("name", sender.get("address", "Unknown")),
            "from_email":     sender.get("address", ""),
            "preview":        msg.get("bodyPreview", "")[:150],
            "time":           _format_time(received_at),
            "receivedAt":     received_at,
            "unread":         not msg.get("isRead", True),
            "conversationId": msg.get("conversationId", ""),
            "importance":     msg.get("importance", "normal"),
            "source":         "outlook_graph",
        })

    return {"emails": emails, "count": len(emails)}


@router.get("/email/{message_id}")
async def get_email_body(message_id: str, user_email: str = "default"):
    """Get full email body."""
    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}?$select=id,subject,from,body,receivedDateTime,toRecipients,ccRecipients"

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
    data       = await request.json()
    user_email = data.get("user_email", "default")
    to         = data.get("to", "")
    subject    = data.get("subject", "")
    body       = data.get("body", "")
    cc         = data.get("cc", "")

    if not all([to, subject, body]):
        return JSONResponse({"error": "Missing to, subject, or body"}, status_code=400)

    access_token = await get_valid_access_token(user_email)
    if not access_token:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    message = {
        "subject": subject,
        "body": {
            "contentType": "Text",
            "content": body,
        },
        "toRecipients": [{"emailAddress": {"address": addr.strip()}} for addr in to.split(",") if addr.strip()],
    }

    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": addr.strip()}} for addr in cc.split(",") if addr.strip()]

    payload = {"message": message, "saveToSentItems": True}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if resp.status_code == 202:
        print(f"📤 Email sent to {to} by {user_email}")
        return {"success": True}
    else:
        return JSONResponse({"error": "send_failed", "detail": resp.text}, status_code=500)


@router.post("/mark-read/{message_id}")
async def mark_read(message_id: str, request: Request):
    """Mark email as read."""
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


# ── Helpers ───────────────────────────────────────────────────────────────────

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
