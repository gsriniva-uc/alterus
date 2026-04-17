"""
transcript_analyzer.py
Analyzes Zoom meeting transcripts using LLM to extract:
  - Action items (who, what, by when)
  - Decisions made
  - Follow-up emails needed
  - Key discussion topics
  - Sentiment/energy of the meeting

Uses Ollama llama3.2 — fully local, no API cost.
"""

import sys
import json
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.drafter import generate
from agent.persona import build_system_prompt


# ── Extraction prompts ────────────────────────────────────────────────────────

ACTION_ITEMS_PROMPT = """You extract structured data from meeting transcripts.
Return ONLY valid JSON. No explanation, no markdown fences."""

ACTION_ITEMS_USER = """From this meeting transcript, extract ALL action items, decisions, and follow-ups.

Meeting: {title}
Date: {date}
Participants: {participants}

TRANSCRIPT:
{transcript}

Return ONLY this JSON (no other text):
{{
  "action_items": [
    {{
      "owner": "person responsible",
      "action": "specific task description",
      "deadline": "by when (or 'not specified')",
      "priority": "high/medium/low"
    }}
  ],
  "decisions": [
    {{
      "decision": "what was decided",
      "made_by": "who decided or 'group'"
    }}
  ],
  "follow_ups": [
    {{
      "to": "person to follow up with",
      "regarding": "what topic",
      "type": "email/teams/meeting"
    }}
  ],
  "key_topics": ["topic 1", "topic 2", "topic 3"],
  "meeting_sentiment": "productive/neutral/tense/inconclusive",
  "summary": "2-3 sentence meeting summary"
}}"""


FOLLOWUP_EMAIL_PROMPT = """You are Ganesh Srinivasan, Principal PM at ServiceNow EAI team.
Write a concise follow-up email after a meeting. Be direct, action-oriented.
Use your actual voice — no fluff, no filler."""

FOLLOWUP_EMAIL_USER = """Write a follow-up email after this meeting:

Meeting: {title}
Date: {date}
Attendees: {participants}

Summary: {summary}

Action items:
{action_items_text}

Decisions made:
{decisions_text}

Write a professional follow-up email that:
1. Thanks attendees (one sentence, not sycophantic)
2. Summarizes the 2-3 key decisions
3. Lists action items clearly with owners and deadlines
4. States next steps
5. Signs off as Ganesh

Keep it under 200 words. Direct and clear."""


# ── Analyzer ─────────────────────────────────────────────────────────────────

class TranscriptAnalyzer:

    def extract_participants(self, segments: list[dict]) -> list[str]:
        """Extract unique speaker names from transcript segments."""
        speakers = set()
        for seg in segments:
            spk = seg.get("speaker", "").strip()
            if spk and spk != "Unknown" and len(spk) < 50:
                speakers.add(spk)
        return sorted(list(speakers))

    def analyze(
        self,
        transcript_text: str,
        segments:        list[dict],
        meeting_title:   str,
        meeting_date:    str,
    ) -> dict:
        """
        Full analysis of a meeting transcript.
        Returns structured dict with action items, decisions, follow-ups.
        """
        participants = self.extract_participants(segments)
        participants_str = ", ".join(participants) if participants else "Unknown"

        print(f"   👥 Participants: {participants_str}")
        print(f"   📝 Transcript length: {len(transcript_text.split())} words")

        # Truncate very long transcripts to fit in context
        max_words  = 2000
        words      = transcript_text.split()
        if len(words) > max_words:
            # Take beginning + end (most important parts)
            half      = max_words // 2
            truncated = " ".join(words[:half]) + "\n\n[...middle truncated...]\n\n" + " ".join(words[-half:])
            print(f"   ✂️  Transcript truncated to {max_words} words for analysis")
        else:
            truncated = transcript_text

        # ── Extract action items, decisions, follow-ups ───────────────────────
        print("   🧠 Extracting action items and decisions...")
        raw = generate(
            system_prompt = ACTION_ITEMS_PROMPT,
            user_message  = ACTION_ITEMS_USER.format(
                title        = meeting_title,
                date         = meeting_date,
                participants = participants_str,
                transcript   = truncated,
            ),
            temperature = 0.1,
        )

        # Parse JSON
        analysis = self._parse_json(raw, {
            "action_items":      [],
            "decisions":         [],
            "follow_ups":        [],
            "key_topics":        [],
            "meeting_sentiment": "neutral",
            "summary":           "Meeting analysis unavailable.",
        })

        # ── Generate follow-up email ──────────────────────────────────────────
        print("   ✍️  Drafting follow-up email...")
        action_items_text = "\n".join([
            f"- [{item.get('owner','?')}] {item.get('action','?')} "
            f"(by {item.get('deadline','TBD')}) [{item.get('priority','medium')}]"
            for item in analysis.get("action_items", [])
        ]) or "No action items identified."

        decisions_text = "\n".join([
            f"- {d.get('decision','?')}"
            for d in analysis.get("decisions", [])
        ]) or "No formal decisions recorded."

        followup_email = generate(
            system_prompt = FOLLOWUP_EMAIL_PROMPT,
            user_message  = FOLLOWUP_EMAIL_USER.format(
                title            = meeting_title,
                date             = meeting_date,
                participants     = participants_str,
                summary          = analysis.get("summary", ""),
                action_items_text = action_items_text,
                decisions_text   = decisions_text,
            ),
            temperature = 0.7,
        )

        # ── Assemble result ───────────────────────────────────────────────────
        result = {
            "meeting_title":    meeting_title,
            "meeting_date":     meeting_date,
            "participants":     participants,
            "analyzed_at":      datetime.now().isoformat(),
            "summary":          analysis.get("summary", ""),
            "action_items":     analysis.get("action_items", []),
            "decisions":        analysis.get("decisions", []),
            "follow_ups":       analysis.get("follow_ups", []),
            "key_topics":       analysis.get("key_topics", []),
            "meeting_sentiment": analysis.get("meeting_sentiment", "neutral"),
            "followup_email":   followup_email,
            "transcript_preview": transcript_text[:500],
        }

        print(f"   ✅ Found {len(result['action_items'])} action items, "
              f"{len(result['decisions'])} decisions")

        return result

    def _parse_json(self, text: str, fallback: dict) -> dict:
        """Parse JSON from LLM output with fallback."""
        try:
            clean = text.strip()
            if clean.startswith("```"):
                lines = clean.split("\n")
                clean = "\n".join(lines[1:-1] if lines[-1].strip()=="```" else lines[1:])
            start = clean.find("{")
            end   = clean.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(clean[start:end])
        except Exception as e:
            print(f"   ⚠️  JSON parse failed: {e}")
        return fallback


# ── Process a meeting end-to-end ──────────────────────────────────────────────

def process_meeting(meeting: dict) -> dict:
    """
    Full pipeline: load transcript → analyze → return results.
    Called by zoom_watcher when new meeting detected.
    """
    from channels.zoom_watcher import (
        load_meeting_transcript, load_chat_log, mark_processed
    )

    print(f"\n📋 Processing: {meeting['title']} ({meeting['date']})")

    segments, text = load_meeting_transcript(meeting)

    if not text:
        print("   ⚠️  No transcript content found")
        return {
            **meeting,
            "error": "No transcript content",
            "analyzed_at": datetime.now().isoformat(),
        }

    chat_log = load_chat_log(meeting)
    if chat_log:
        text += f"\n\n[CHAT LOG]\n{chat_log[:500]}"

    analyzer = TranscriptAnalyzer()
    result   = analyzer.analyze(
        transcript_text = text,
        segments        = segments,
        meeting_title   = meeting["title"],
        meeting_date    = meeting["date"],
    )

    # Merge meeting metadata with analysis
    full_result = {**meeting, **result}

    # Mark as processed
    mark_processed(meeting["id"])

    return full_result


def process_all_new_meetings() -> list[dict]:
    """Process all unprocessed meetings with transcripts."""
    from channels.zoom_watcher import get_new_meetings, load_meetings_cache, save_meetings_cache

    new_meetings = get_new_meetings()

    if not new_meetings:
        print("No new meetings to process.")
        return []

    print(f"\n🎯 Processing {len(new_meetings)} new meeting(s)...\n")
    results  = []
    existing = load_meetings_cache()

    for meeting in new_meetings:
        result = process_meeting(meeting)
        results.append(result)

    # Append to cache
    all_meetings = existing + results
    save_meetings_cache(all_meetings)

    return results


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║   Transcript Analyzer — Test             ║")
    print("╚══════════════════════════════════════════╝\n")

    # Test with a mock transcript
    mock_transcript = """
[Ganesh Srinivasan]
Good morning everyone. Let's get through the sprint planning quickly.
We need to resolve the NBA agent architecture decision today.

[Jerry Jiang]
I think we should go with the single router approach.
It's simpler and we've already validated it in staging.

[Ganesh Srinivasan]
Agreed. Let's lock that in. Jerry, can you update the architecture doc by Thursday?

[Sibanjan Das]
I'll need the API specs from Jerry before I can start implementation.
Targeting Friday for the first PR.

[Ganesh Srinivasan]
Perfect. Senthil, what's the status on the three overdue items?

[Senthil V.]
Two are unblocked now. The third needs a decision on the Stardog vs Neo4j evaluation.
I'd say we need that by end of week.

[Ganesh Srinivasan]
I'll make a recommendation to Raghu by Wednesday.
Let's wrap up. Key actions: Jerry updates arch doc Thursday,
Sibanjan first PR Friday, I handle Stardog decision Wednesday.
"""

    analyzer = TranscriptAnalyzer()
    result   = analyzer.analyze(
        transcript_text = mock_transcript,
        segments        = [],
        meeting_title   = "EAI Sprint 14 Planning",
        meeting_date    = datetime.now().strftime("%Y-%m-%d"),
    )

    print("\n── SUMMARY ───────────────────────────────────")
    print(result["summary"])

    print("\n── ACTION ITEMS ──────────────────────────────")
    for item in result["action_items"]:
        print(f"  [{item.get('priority','?').upper()}] {item.get('owner','?')}: "
              f"{item.get('action','?')} (by {item.get('deadline','TBD')})")

    print("\n── DECISIONS ─────────────────────────────────")
    for d in result["decisions"]:
        print(f"  • {d.get('decision','?')}")

    print("\n── FOLLOW-UP EMAIL ───────────────────────────")
    print(result["followup_email"])
