"""
drafter.py
Generates drafts using Ollama llama3.2 (local, free).
Takes system prompt from persona.py + retrieved context + input message.
"""

import sys
import requests
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_CHAT  = "http://localhost:11434/api/chat"
LLM_MODEL    = "llama3.2"
TEMPERATURE  = 0.7
MAX_TOKENS   = 2048


def generate(
    system_prompt: str,
    user_message:  str,
    temperature:   float = TEMPERATURE,
) -> str:
    """
    Generate a response using Ollama llama3.2.
    Uses chat endpoint for proper system/user role separation.
    """
    payload = {
        "model":  LLM_MODEL,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": MAX_TOKENS,
        },
        "messages": [
            {"role": "system",  "content": system_prompt},
            {"role": "user",    "content": user_message},
        ]
    }

    try:
        resp = requests.post(OLLAMA_CHAT, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    except requests.exceptions.Timeout:
        return "ERROR: LLM timed out. Try again."
    except Exception as e:
        return f"ERROR: {e}"


def draft_response(
    input_text:    str,
    system_prompt: str,
    context:       str = "",
    task_type:     str = "reply",
) -> str:
    """
    Generate a draft response in Ganesh's voice.
    Combines input, retrieved context, and task instructions.
    """
    user_message = f"""
INPUT MESSAGE / TASK:
{input_text}

RETRIEVED CONTEXT FROM YOUR PAST WORK:
{context if context else "No specific context retrieved."}

INSTRUCTIONS:
Based on the above, draft a {task_type} in Ganesh's voice.
Use the retrieved context to match tone, style, and relevant details.
Be direct, clear, and action-oriented.
Do not add unnecessary preamble — just write the draft.
""".strip()

    return generate(system_prompt, user_message)


def check_ollama() -> bool:
    """Verify Ollama is running with llama3.2."""
    try:
        resp   = requests.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        if not any("llama3.2" in m for m in models):
            print("❌ llama3.2 not found. Run: ollama pull llama3.2")
            return False
        return True
    except Exception:
        print("❌ Ollama not running. Start: ollama serve")
        return False


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not check_ollama():
        exit(1)

    from agent.persona import build_system_prompt

    print("── Test: Draft email to VP ───────────────────")
    system = build_system_prompt("email")
    draft  = draft_response(
        input_text  = "Jason Wong asked for a quick status update on Customer Engine",
        system_prompt = system,
        task_type   = "email",
    )
    print(draft)
