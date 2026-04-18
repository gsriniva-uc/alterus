"""
drafter.py
Generates drafts using either:
  - Anthropic Claude API (cloud, recommended for Railway)
  - Ollama llama3.2 (local, for development on MacBook)

Set ANTHROPIC_API_KEY in .env to use Claude.
Falls back to Ollama automatically if key not set.
"""

import os
import sys
import requests
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OLLAMA_URL        = "http://localhost:11434/api/chat"
OLLAMA_MODEL      = "llama3.2"
CLAUDE_MODEL      = "claude-sonnet-4-5"
TEMPERATURE       = 0.7
MAX_TOKENS        = 2048

USE_CLAUDE = bool(ANTHROPIC_API_KEY and ANTHROPIC_API_KEY.startswith("sk-ant-"))


def generate(
    system_prompt: str,
    user_message:  str,
    temperature:   float = TEMPERATURE,
) -> str:
    """
    Generate a response using Claude API (cloud) or Ollama (local).
    Automatically picks Claude if ANTHROPIC_API_KEY is set.
    """
    if USE_CLAUDE:
        return _generate_claude(system_prompt, user_message, temperature)
    else:
        return _generate_ollama(system_prompt, user_message, temperature)


def _generate_claude(system_prompt: str, user_message: str, temperature: float) -> str:
    """Generate using Anthropic Claude API."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model      = CLAUDE_MODEL,
            max_tokens = MAX_TOKENS,
            system     = system_prompt,
            messages   = [{"role": "user", "content": user_message}],
        )
        return message.content[0].text
    except Exception as e:
        print(f"Claude API error: {e} — falling back to Ollama")
        return _generate_ollama(system_prompt, user_message, temperature)


def _generate_ollama(system_prompt: str, user_message: str, temperature: float) -> str:
    """Generate using local Ollama."""
    try:
        payload = {
            "model":  OLLAMA_MODEL,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": MAX_TOKENS},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ]
        }
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except Exception as e:
        return f"[Draft unavailable — LLM error: {e}]"


# ── Model info helper ─────────────────────────────────────────────────────────
def get_model_info() -> dict:
    return {
        "model":    CLAUDE_MODEL if USE_CLAUDE else OLLAMA_MODEL,
        "provider": "Claude API" if USE_CLAUDE else "Ollama (local)",
        "status":   "✅ connected" if USE_CLAUDE else "🟡 local only",
    }
