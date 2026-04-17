"""
mailto_sender.py
Generates mailto: deep links for one-click email sending via Outlook.
No Graph API needed — opens Outlook with pre-filled draft.

Usage:
    from channels.mailto_sender import make_mailto_link, make_teams_deeplink
"""

import urllib.parse
import re


def make_mailto_link(
    to:      str,
    subject: str,
    body:    str,
    cc:      str = "",
) -> str:
    """
    Generate a mailto: deep link that opens Outlook with pre-filled draft.
    User clicks → Outlook opens → one click to send.

    Args:
        to:      recipient email address
        subject: email subject line
        body:    email body text
        cc:      optional CC addresses

    Returns:
        mailto: URL string
    """
    params = {
        "subject": subject,
        "body":    body,
    }
    if cc:
        params["cc"] = cc

    query_string = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    to_encoded   = urllib.parse.quote(to)

    return f"mailto:{to_encoded}?{query_string}"


def make_outlook_web_link(
    to:      str,
    subject: str,
    body:    str,
) -> str:
    """
    Generate an Outlook Web App (OWA) deep link.
    Works when Outlook desktop isn't configured.
    Opens new compose window in browser.
    """
    base = "https://outlook.office.com/mail/deeplink/compose"
    params = {
        "to":      to,
        "subject": subject,
        "body":    body,
    }
    return f"{base}?{urllib.parse.urlencode(params)}"


def extract_recipient_from_draft(draft: str, sender_email: str = "") -> str:
    """
    Try to extract recipient email from a draft.
    Falls back to sender_email if known.
    """
    # Look for explicit To: line
    to_match = re.search(r"^To:\s*(.+)$", draft, re.MULTILINE | re.IGNORECASE)
    if to_match:
        return to_match.group(1).strip()

    # Use known sender email
    if sender_email:
        return sender_email

    return ""


def extract_subject_from_draft(draft: str, fallback: str = "") -> str:
    """
    Try to extract subject from a draft email.
    Looks for 'Subject:' line or uses the fallback.
    """
    # Look for Subject: line
    subj_match = re.search(r"^Subject:\s*(.+)$", draft, re.MULTILINE | re.IGNORECASE)
    if subj_match:
        return subj_match.group(1).strip()

    # Try first non-empty line as subject
    lines = [l.strip() for l in draft.split("\n") if l.strip()]
    if lines:
        first = lines[0]
        if len(first) < 100 and not first.startswith("["):
            return first

    return fallback


def clean_draft_for_mailto(draft: str) -> str:
    """
    Clean a draft for mailto: body.
    Removes Subject:/To: header lines if present.
    """
    lines = draft.split("\n")
    cleaned = []
    skip_headers = True

    for line in lines:
        if skip_headers and re.match(r"^(To|CC|BCC|Subject|From):", line, re.IGNORECASE):
            continue
        else:
            skip_headers = False
            cleaned.append(line)

    return "\n".join(cleaned).strip()


def build_mailto_from_result(
    agent_result: dict,
    sender_email: str = "",
    original_subject: str = "",
) -> dict:
    """
    Build a complete mailto link from an agent draft result.

    Returns dict with:
        mailto_link:      full mailto: URL
        outlook_web_link: OWA fallback URL
        to:               recipient
        subject:          subject line
        body_preview:     first 100 chars of body
    """
    draft    = agent_result.get("final_draft", "")
    audience = agent_result.get("classification", {}).get("audience", "peer")

    # Extract components
    to      = extract_recipient_from_draft(draft, sender_email)
    subject = extract_subject_from_draft(draft, f"Re: {original_subject}" if original_subject else "Follow-up")
    body    = clean_draft_for_mailto(draft)

    # Build links
    mailto_link      = make_mailto_link(to, subject, body)
    outlook_web_link = make_outlook_web_link(to, subject, body)

    return {
        "mailto_link":      mailto_link,
        "outlook_web_link": outlook_web_link,
        "to":               to,
        "subject":          subject,
        "body":             body,
        "body_preview":     body[:100] + "..." if len(body) > 100 else body,
    }


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test mailto link generation
    link = make_mailto_link(
        to      = "jason.wong@servicenow.com",
        subject = "Customer Engine Q2 Update",
        body    = "Hi Jason,\n\nQuick update on Customer Engine...\n\nBest,\nGanesh"
    )
    print("mailto link:")
    print(link[:100] + "...")

    owa_link = make_outlook_web_link(
        to      = "jason.wong@servicenow.com",
        subject = "Customer Engine Q2 Update",
        body    = "Hi Jason,\n\nQuick update on Customer Engine...\n\nBest,\nGanesh"
    )
    print("\nOWA link:")
    print(owa_link[:100] + "...")
