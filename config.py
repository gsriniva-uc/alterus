"""
config.py
User configuration — read from environment variables or .env file.
On Railway: set these as environment variables in the dashboard.
Locally: set them in your .env file.

For beta users: each user sets their own environment variables
in their Railway deployment OR you manually set them per user.
"""

import os
from dotenv import load_dotenv
load_dotenv()

# ── User Identity ─────────────────────────────────────────────────────────────
USER_NAME       = os.getenv("USER_NAME",       "Your Name")
USER_TITLE      = os.getenv("USER_TITLE",      "Principal PM")
USER_COMPANY    = os.getenv("USER_COMPANY",    "Your Company")
USER_LOCATION   = os.getenv("USER_LOCATION",  "Boston, MA")
USER_EMAIL      = os.getenv("USER_EMAIL",      "")

# ── Key Stakeholders (comma-separated) ───────────────────────────────────────
# Example: "Jason Wong,Raghu,Sibanjan Das,Jerry Jiang,Senthil V"
_stakeholders_raw = os.getenv("USER_STAKEHOLDERS", "")
USER_STAKEHOLDERS = [s.strip() for s in _stakeholders_raw.split(",") if s.strip()]

# ── Key Workstreams (comma-separated) ────────────────────────────────────────
_workstreams_raw = os.getenv("USER_WORKSTREAMS", "")
USER_WORKSTREAMS = [w.strip() for w in _workstreams_raw.split(",") if w.strip()]

# ── Communication Style ───────────────────────────────────────────────────────
# direct / balanced / diplomatic
USER_TONE       = os.getenv("USER_TONE", "balanced")

# ── LLM ──────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── LangSmith ────────────────────────────────────────────────────────────────
LANGSMITH_API_KEY   = os.getenv("LANGSMITH_API_KEY", "")
LANGSMITH_TRACING   = os.getenv("LANGSMITH_TRACING", "false").lower() == "true"
LANGSMITH_PROJECT   = os.getenv("LANGSMITH_PROJECT", "alterus")

# ── Webhook Server ────────────────────────────────────────────────────────────
WEBHOOK_PORT    = int(os.getenv("WEBHOOK_PORT", "8000"))
NGROK_URL       = os.getenv("NGROK_URL", "")

# ── Data paths ────────────────────────────────────────────────────────────────
import os as _os
DATA_DIR        = os.getenv("DATA_DIR", "data")


def get_persona_summary() -> str:
    """Return a one-line summary of the user's persona for display."""
    return f"{USER_NAME} · {USER_TITLE} · {USER_COMPANY} · {USER_LOCATION}"


def is_configured() -> bool:
    """Return True if the user has set their basic config."""
    return USER_NAME != "Your Name" and bool(USER_STAKEHOLDERS)


def print_config():
    """Print current config for debugging."""
    print(f"User:         {USER_NAME}")
    print(f"Title:        {USER_TITLE}")
    print(f"Company:      {USER_COMPANY}")
    print(f"Stakeholders: {USER_STAKEHOLDERS}")
    print(f"LLM:          {'Claude API' if ANTHROPIC_API_KEY else 'Ollama (local)'}")
    print(f"LangSmith:    {'enabled' if LANGSMITH_TRACING else 'disabled'}")


if __name__ == "__main__":
    print_config()
