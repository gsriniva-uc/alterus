"""
graph_subscriptions.py
Microsoft Graph Change Notification subscription manager for Alterus.

Replaces Power Automate + ngrok entirely.

HOW IT WORKS:
  1. User connects Outlook (OAuth callback)
  2. We immediately register a Graph subscription on their mailbox:
       POST https://graph.microsoft.com/v1.0/subscriptions
       → Graph now pushes to https://alterus.onrender.com/webhook/email
         every time a new email arrives for this user
  3. Subscriptions expire after ~3 days — background task renews them
  4. On disconnect, subscription is deleted

WHAT THE USER DOES:
  Click "Connect Outlook". Nothing else. Ever.

RESOURCES SUBSCRIBED:
  - Email:    me/mailFolders/inbox/messages  (new emails)
  - Calendar: me/events                      (new/updated events)
  - Teams:    polling only (v1) — delegated permissions can't subscribe
              to all chats without admin consent. See poll_teams().
"""

import os
import json
import asyncio
import httpx
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────

# Your Render deployment URL — Graph POSTs notifications here directly.
# No ngrok. No Power Automate.
WEBHOOK_BASE  = os.getenv("WEBHOOK_BASE_URL", "https://alterus.onrender.com")
CLIENT_ID     = os.getenv("AZURE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
TENANT_ID     = os.getenv("AZURE_TENANT_ID", "common")

# Where we store active subscriptions so we can renew them
DATA_DIR      = Path("data")
SUBS_FILE     = DATA_DIR / "graph_subscriptions.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Graph subscription max lifetimes (minutes)
# Mail and calendar: 4230 min (~2.9 days). We renew at 2 days to be safe.
MAIL_EXPIRY_MINUTES     = 4230
CALENDAR_EXPIRY_MINUTES = 4230
RENEW_BEFORE_MINUTES    = 60 * 24 * 2   # Renew when < 2 days remain

GRAPH_SUBS_URL = "https://graph.microsoft.com/v1.0/subscriptions"


# ── Subscription storage ──────────────────────────────────────────────────────
# Structure: { subscription_id: { user_email, resource, expiry_dt, ... } }

def _load_subs() -> dict:
    if not SUBS_FILE.exists():
        return {}
    try:
        return json.loads(SUBS_FILE.read_text())
    except Exception:
        return {}


def _save_subs(subs: dict):
    SUBS_FILE.write_text(json.dumps(subs, indent=2))


def get_user_email_for_subscription(subscription_id: str) -> str | None:
    """
    Given a subscription ID from a Graph notification, return the user's email.
    This is how the webhook handler knows WHOSE email just arrived.
    """
    subs = _load_subs()
    entry = subs.get(subscription_id)
    return entry["user_email"] if entry else None


def get_subscriptions_for_user(user_email: str) -> list[dict]:
    """Return all active subscriptions for a user."""
    subs = _load_subs()
    return [
        {"id": sid, **data}
        for sid, data in subs.items()
        if data.get("user_email") == user_email
    ]


# ── Register subscriptions ────────────────────────────────────────────────────

async def register_all_subscriptions(user_email: str, access_token: str) -> dict:
    """
    Called automatically after OAuth callback.
    Registers email + calendar subscriptions for this user.
    Returns a summary of what was registered.

    The user never sees this happening — it runs in the background.
    """
    results = {}

    # Delete any stale existing subscriptions first
    await delete_subscriptions_for_user(user_email, access_token)

    # Register email subscription
    email_sub = await _register_subscription(
        access_token  = access_token,
        user_email    = user_email,
        resource      = "me/mailFolders/inbox/messages",
        change_types  = "created",
        notify_url    = f"{WEBHOOK_BASE}/webhook/email",
        expiry_minutes= MAIL_EXPIRY_MINUTES,
        label         = "email",
    )
    results["email"] = email_sub

    # Register calendar subscription
    cal_sub = await _register_subscription(
        access_token  = access_token,
        user_email    = user_email,
        resource      = "me/events",
        change_types  = "created,updated",
        notify_url    = f"{WEBHOOK_BASE}/webhook/calendar",
        expiry_minutes= CALENDAR_EXPIRY_MINUTES,
        label         = "calendar",
    )
    results["calendar"] = cal_sub

    return results


async def _register_subscription(
    access_token: str,
    user_email: str,
    resource: str,
    change_types: str,
    notify_url: str,
    expiry_minutes: int,
    label: str,
) -> dict:
    """
    Register a single Graph change notification subscription.

    Graph validates our notificationUrl by sending a POST with
    ?validationToken=<token> — our webhook handlers return that token
    as plain text. Graph then starts sending real notifications.
    """
    expiry_dt = (
        datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    payload = {
        "changeType":             change_types,
        "notificationUrl":        notify_url,
        "resource":               resource,
        "expirationDateTime":     expiry_dt,
        # clientState = user_email: echoed back in every notification
        # so webhook handlers know whose data just arrived
        "clientState":            user_email,
        "latestSupportedTlsVersion": "v1_2",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            GRAPH_SUBS_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            json=payload,
        )

    if resp.status_code == 201:
        sub_data = resp.json()
        sub_id   = sub_data["id"]

        # Store so we can renew it later
        subs = _load_subs()
        subs[sub_id] = {
            "user_email":  user_email,
            "resource":    resource,
            "label":       label,
            "notify_url":  notify_url,
            "expiry_dt":   expiry_dt,
            "registered":  datetime.now(timezone.utc).isoformat(),
        }
        _save_subs(subs)

        print(f"✅ Graph subscription registered [{label}] for {user_email} → {notify_url}")
        return {"success": True, "subscription_id": sub_id, "expires": expiry_dt}

    else:
        err = resp.text[:200]
        print(f"⚠️  Graph subscription failed [{label}] for {user_email}: {resp.status_code} {err}")
        return {"success": False, "error": err, "status_code": resp.status_code}


# ── Renew subscriptions ───────────────────────────────────────────────────────

async def renew_expiring_subscriptions():
    """
    Background task — runs every hour.
    Renews any subscriptions expiring within RENEW_BEFORE_MINUTES.

    Graph subscriptions that aren't renewed simply stop sending notifications
    silently — no error, emails just stop appearing in Alterus.
    This task prevents that.
    """
    subs     = _load_subs()
    now      = datetime.now(timezone.utc)
    renewed  = 0
    failed   = 0

    for sub_id, data in list(subs.items()):
        try:
            expiry = datetime.fromisoformat(
                data["expiry_dt"].replace("Z", "+00:00").replace(".0000000+", "+")
            )
            time_left = expiry - now

            if time_left.total_seconds() < RENEW_BEFORE_MINUTES * 60:
                user_email = data["user_email"]
                print(f"🔄 Renewing Graph subscription [{data['label']}] for {user_email}")

                # Get a fresh access token for this user
                from channels.token_store import load_token
                token = load_token(user_email)
                if not token:
                    print(f"⚠️  Cannot renew subscription for {user_email} — no token")
                    continue

                from channels.outlook_connector import get_valid_access_token
                access_token = await get_valid_access_token(user_email)
                if not access_token:
                    print(f"⚠️  Cannot renew subscription for {user_email} — token invalid")
                    continue

                new_expiry = (
                    now + timedelta(minutes=MAIL_EXPIRY_MINUTES)
                ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.patch(
                        f"{GRAPH_SUBS_URL}/{sub_id}",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type":  "application/json",
                        },
                        json={"expirationDateTime": new_expiry},
                    )

                if resp.status_code == 200:
                    subs[sub_id]["expiry_dt"] = new_expiry
                    renewed += 1
                    print(f"✅ Renewed subscription [{data['label']}] for {user_email}")
                else:
                    failed += 1
                    print(f"❌ Renewal failed for {sub_id}: {resp.status_code} {resp.text[:100]}")

        except Exception as e:
            failed += 1
            print(f"❌ Error processing subscription {sub_id}: {e}")

    if renewed or failed:
        _save_subs(subs)
        print(f"🔄 Subscription renewal: {renewed} renewed, {failed} failed")


async def subscription_renewal_loop():
    """
    Infinite background loop that checks for expiring subscriptions every hour.
    Started on FastAPI startup.
    """
    print("🔄 Graph subscription renewal loop started")
    while True:
        try:
            await renew_expiring_subscriptions()
        except Exception as e:
            print(f"❌ Renewal loop error: {e}")
        await asyncio.sleep(3600)  # check every hour


# ── Delete subscriptions ──────────────────────────────────────────────────────

async def delete_subscriptions_for_user(user_email: str, access_token: str):
    """
    Delete all Graph subscriptions for a user.
    Called on disconnect, and before re-registering on reconnect.
    """
    subs    = _load_subs()
    to_del  = [(sid, d) for sid, d in subs.items() if d.get("user_email") == user_email]

    for sub_id, data in to_del:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(
                    f"{GRAPH_SUBS_URL}/{sub_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            if resp.status_code in (204, 404):
                del subs[sub_id]
                print(f"🗑️  Deleted Graph subscription [{data['label']}] for {user_email}")
            else:
                print(f"⚠️  Could not delete subscription {sub_id}: {resp.status_code}")
        except Exception as e:
            print(f"⚠️  Error deleting subscription {sub_id}: {e}")
            # Remove from local store even if Graph delete failed
            subs.pop(sub_id, None)

    _save_subs(subs)


# ── Teams: polling (v1) ───────────────────────────────────────────────────────

async def poll_teams_messages(user_email: str, access_token: str, limit: int = 20) -> list:
    """
    Teams change notifications require admin-consented application permissions
    for /chats/getAllMessages — not available with per-user delegated OAuth.

    v1 approach: poll on-demand when user opens the Teams tab.
    v2 roadmap:  if customer's IT admin grants ChannelMessage.Read.All,
                 register a subscription the same way email works above.

    This function fetches recent chat messages the user is part of.
    """
    messages = []

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # Step 1: Get list of chats the user is in
            chats_resp = await client.get(
                "https://graph.microsoft.com/v1.0/me/chats"
                "?$select=id,topic,chatType"
                "&$top=10",
                headers={"Authorization": f"Bearer {access_token}"},
            )

            if chats_resp.status_code != 200:
                print(f"Teams poll: could not fetch chats ({chats_resp.status_code})")
                return []

            chats = chats_resp.json().get("value", [])

            # Step 2: For each chat, get recent messages
            for chat in chats[:5]:   # cap at 5 chats to avoid rate limits
                chat_id = chat["id"]
                msgs_resp = await client.get(
                    f"https://graph.microsoft.com/v1.0/me/chats/{chat_id}/messages"
                    f"?$top=5&$orderby=createdDateTime desc",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

                if msgs_resp.status_code != 200:
                    continue

                for msg in msgs_resp.json().get("value", []):
                    sender = msg.get("from", {})
                    sender_name = (
                        sender.get("user", {}).get("displayName")
                        or sender.get("application", {}).get("displayName")
                        or "Unknown"
                    )

                    # Skip own messages and system messages
                    if sender_name in ("Unknown", MY_NAME := os.getenv("USER_DISPLAY_NAME", "")):
                        continue

                    body_content = msg.get("body", {}).get("content", "")

                    messages.append({
                        "id":        msg.get("id", ""),
                        "from":      sender_name,
                        "message":   _strip_html(body_content),
                        "chatId":    chat_id,
                        "chatType":  chat.get("chatType", ""),
                        "topic":     chat.get("topic", "Direct message"),
                        "time":      msg.get("createdDateTime", ""),
                        "source":    "teams_graph",
                    })

    except Exception as e:
        print(f"Teams poll error for {user_email}: {e}")

    return messages[:limit]


def _strip_html(text: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
