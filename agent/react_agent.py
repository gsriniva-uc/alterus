"""
react_agent.py
Real ReAct reasoning for Alterus drafts.

Loop:
  Thought → Action → Observation → Thought → ... → Final Answer

Tools available:
  - search_corpus: retrieve past emails/chats for context
  - analyze_input: extract key facts from incoming message
  - critique_draft: score a draft before returning it
  - get_persona: load user's writing style profile
"""

import os
import re
import json
from pathlib import Path

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MAX_STEPS = 6


def run_react_agent(
    platform: str,
    sender: str,
    subject: str,
    body: str,
    user_name: str,
    user_email: str,
    tone: str = "Balanced",
) -> dict:
    """
    Run ReAct reasoning loop to produce a high-quality draft.
    Returns dict with: draft, reasoning_trace, steps_taken, critique
    """

    # ── Tool implementations ──────────────────────────────────────────────────

    def search_corpus(query: str) -> str:
        """Search user's corpus for relevant past messages."""
        try:
            from retrieval.retriever import Retriever
            retriever = Retriever(user_id=user_email)
            results = retriever.multi_search([query], top_k=3)
            if not results:
                return "No relevant history found in corpus."
            snippets = []
            for r in results[:3]:
                text = r.get("text", "")[:200]
                snippets.append(f"- {text}")
            return "\n".join(snippets)
        except Exception as e:
            return f"Corpus search unavailable: {str(e)}"

    def analyze_input(text: str) -> str:
        """Extract key facts from the incoming message."""
        lines = []
        text_lower = text.lower()

        # Detect urgency
        if any(w in text_lower for w in ["urgent", "asap", "eod", "today", "deadline", "friday", "by end"]):
            lines.append("URGENCY: High — time-sensitive request")
        else:
            lines.append("URGENCY: Normal")

        # Detect message type
        if "?" in text:
            lines.append("TYPE: Question requiring answer")
        elif any(w in text_lower for w in ["please", "can you", "could you", "need"]):
            lines.append("TYPE: Request for action")
        elif any(w in text_lower for w in ["fyi", "update", "status"]):
            lines.append("TYPE: Status/FYI message")
        else:
            lines.append("TYPE: General communication")

        # Detect key topics
        topics = []
        for keyword in ["roadmap", "timeline", "budget", "approval", "review", "meeting", "report", "issue", "blocker", "decision"]:
            if keyword in text_lower:
                topics.append(keyword)
        if topics:
            lines.append(f"KEY TOPICS: {', '.join(topics)}")

        # Word count
        words = len(text.split())
        lines.append(f"LENGTH: {words} words — suggest reply of ~{max(30, words//3)} words")

        return "\n".join(lines)

    def critique_draft(draft: str) -> str:
        """Score draft on key dimensions."""
        scores = {}
        draft_lower = draft.lower()
        draft_words = len(draft.split())

        # Length check
        scores["length"] = "Good" if 20 <= draft_words <= 200 else "Too long" if draft_words > 200 else "Too short"

        # Tone check
        if tone == "Direct":
            scores["tone"] = "Good" if not any(w in draft_lower for w in ["perhaps", "maybe", "if you don't mind"]) else "Too soft"
        elif tone == "Diplomatic":
            scores["tone"] = "Good" if any(w in draft_lower for w in ["appreciate", "thank", "understand", "hope"]) else "Could be warmer"
        else:
            scores["tone"] = "Balanced"

        # Actionability
        scores["actionability"] = "Good" if any(w in draft_lower for w in ["will", "can", "let me", "i'll", "by", "next step"]) else "Needs clear next step"

        # Filler check
        fillers = ["i hope this email finds you well", "as per my last email", "please don't hesitate", "feel free to reach out"]
        scores["filler"] = "Clean" if not any(f in draft_lower for f in fillers) else "Has filler phrases — remove"

        overall = sum(1 for v in scores.values() if v in ["Good", "Balanced", "Clean"]) / len(scores)
        scores["overall"] = f"{int(overall * 100)}%"

        return json.dumps(scores)

    def get_persona() -> str:
        """Get user's writing style + stakeholder profile."""
        base = f"""
Name: {user_name}
Platform: {platform}
Tone setting: {tone}
Style: Direct, data-driven, uses specific numbers and names
Avoids: filler phrases, excessive pleasantries, passive voice
Typical length: 50-150 words for emails, 20-60 words for Teams
Signature style: Gets to the point fast, ends with clear next step
"""
        # Enrich with stakeholder profile if available
        if sender:
            try:
                from agent.stakeholder_intelligence import get_profile
                profile = get_profile(user_email, sender.split()[0])
                if profile and profile.get("draft_instruction"):
                    base += f"""
STAKEHOLDER PROFILE for {sender}:
- Communication style: {profile.get("communication_style", "unknown")}
- Preferred tone: {profile.get("preferred_tone", "")}
- Draft instruction: {profile.get("draft_instruction", "")}
- Avoid: {", ".join(profile.get("avoid", []))}
"""
            except Exception:
                pass
        return base

    # ── ReAct system prompt ───────────────────────────────────────────────────
    system_prompt = f"""You are {user_name}'s AI writing assistant using ReAct reasoning.

You have access to these tools:
- search_corpus(query): Search past messages for context
- analyze_input(text): Extract key facts from incoming message
- critique_draft(draft): Score a draft before finalizing
- get_persona(): Get writing style profile

Use this EXACT format for each step:
Thought: [your reasoning]
Action: tool_name(input)
Observation: [tool result will be inserted here]

After gathering enough context, write:
Thought: I have enough context to write a high-quality draft.
Final Answer: [the actual draft only, no preamble]

Rules:
- Maximum {MAX_STEPS} steps before Final Answer
- Final Answer must be ONLY the draft text, nothing else
- Match {user_name}'s voice exactly — direct, specific, no fluff
- Tone: {tone}
- Platform: {platform}
"""

    user_message = f"""Draft a reply to this {platform} message:

From: {sender}
Subject: {subject}
Message: {body}

Start reasoning now."""

    # ── ReAct loop ────────────────────────────────────────────────────────────
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    messages = [{"role": "user", "content": user_message}]
    reasoning_trace = []
    steps = 0
    final_draft = ""

    while steps < MAX_STEPS:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=system_prompt,
            messages=messages,
        )

        assistant_text = response.content[0].text
        messages.append({"role": "assistant", "content": assistant_text})
        reasoning_trace.append(assistant_text)

        # Check for Final Answer
        if "Final Answer:" in assistant_text:
            final_draft = assistant_text.split("Final Answer:")[-1].strip()
            break

        # Parse and execute tool calls
        action_match = re.search(r'Action:\s*(\w+)\(([^)]*)\)', assistant_text)
        if action_match:
            tool_name = action_match.group(1)
            tool_input = action_match.group(2).strip('"\'')

            # Execute tool
            if tool_name == "search_corpus":
                observation = search_corpus(tool_input)
            elif tool_name == "analyze_input":
                observation = analyze_input(tool_input or body)
            elif tool_name == "critique_draft":
                observation = critique_draft(tool_input)
            elif tool_name == "get_persona":
                observation = get_persona()
            else:
                observation = f"Unknown tool: {tool_name}"

            # Add observation to messages
            messages.append({
                "role": "user",
                "content": f"Observation: {observation}"
            })
            reasoning_trace.append(f"Observation: {observation}")
        else:
            # No action found, prompt to continue
            messages.append({
                "role": "user",
                "content": "Continue reasoning or provide Final Answer."
            })

        steps += 1

    # Fallback if no final answer
    if not final_draft:
        from agent.drafter import generate
        from agent.persona import build_system_prompt
        sp = build_system_prompt(platform, {"name": user_name, "email": user_email})
        final_draft = generate(sp, f"Reply to this {platform} message from {sender}: {body}", temperature=0.7)

    # Get critique of final draft
    critique_raw = critique_draft(final_draft)
    try:
        critique = json.loads(critique_raw)
    except Exception:
        critique = {}

    return {
        "draft": final_draft,
        "reasoning_trace": reasoning_trace,
        "steps_taken": steps,
        "critique": critique,
        "agent": "react",
    }
