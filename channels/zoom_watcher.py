"""
zoom_watcher.py
Watches ~/Documents/Zoom for new meeting folders.
Parses .vtt transcript files and queues them for analysis.

Run as background service:
    python -m channels.zoom_watcher

Or call process_all_meetings() directly from the UI.
"""

import sys
import re
import json
import time
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
ZOOM_DIR      = Path.home() / "Documents" / "Zoom"
PROCESSED_LOG = Path("data/zoom_processed.json")
MEETINGS_CACHE = Path("data/zoom_meetings.json")


# ── VTT Parser ────────────────────────────────────────────────────────────────

def parse_vtt(vtt_path: Path) -> list[dict]:
    """
    Parse a WebVTT (.vtt) transcript file into a list of utterances.
    Each utterance: {speaker, text, start_time, end_time}
    """
    content  = vtt_path.read_text(encoding="utf-8", errors="ignore")
    lines    = content.strip().split("\n")
    segments = []
    i        = 0

    # Skip WEBVTT header
    while i < len(lines) and not lines[i].strip().startswith("0") and "-->" not in lines[i]:
        i += 1

    while i < len(lines):
        line = lines[i].strip()

        # Skip sequence numbers and blank lines
        if not line or line.isdigit():
            i += 1
            continue

        # Timestamp line: 00:01:23.456 --> 00:01:25.789
        if "-->" in line:
            time_parts = line.split("-->")
            start_time = time_parts[0].strip()
            end_time   = time_parts[1].strip().split()[0]  # ignore alignment tags

            i += 1
            text_lines = []

            # Collect text lines until blank line or end
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i].strip())
                i += 1

            full_text = " ".join(text_lines)

            # Extract speaker if present (format: "Speaker Name: text")
            speaker = "Unknown"
            text    = full_text
            if ":" in full_text:
                parts = full_text.split(":", 1)
                # Speaker names are typically short (< 40 chars)
                if len(parts[0]) < 40 and not parts[0].strip().startswith("<"):
                    speaker = parts[0].strip()
                    text    = parts[1].strip()

            # Clean HTML tags from text
            text = re.sub(r"<[^>]+>", "", text).strip()

            if text:
                segments.append({
                    "speaker":    speaker,
                    "text":       text,
                    "start_time": start_time,
                    "end_time":   end_time,
                })
        else:
            i += 1

    return segments


def vtt_to_transcript_text(segments: list[dict]) -> str:
    """Convert parsed VTT segments into clean readable transcript."""
    lines    = []
    last_spk = None

    for seg in segments:
        if seg["speaker"] != last_spk:
            lines.append(f"\n[{seg['speaker']}]")
            last_spk = seg["speaker"]
        lines.append(seg["text"])

    return "\n".join(lines).strip()


# ── Meeting folder scanner ────────────────────────────────────────────────────

def scan_zoom_folder() -> list[dict]:
    """
    Scan ~/Documents/Zoom for meeting folders.
    Returns list of meeting dicts with metadata and transcript path.
    """
    if not ZOOM_DIR.exists():
        return []

    meetings = []

    for folder in sorted(ZOOM_DIR.iterdir(), reverse=True):
        if not folder.is_dir():
            continue

        # Find transcript files (.vtt or closed_caption.vtt)
        vtt_files = list(folder.glob("*.vtt")) + list(folder.glob("**/*.vtt"))
        txt_files = list(folder.glob("*.txt"))  # chat logs
        audio_files = list(folder.glob("*.m4a")) + list(folder.glob("*.mp4"))

        # Parse meeting date from folder name
        # Zoom format: "YYYY-MM-DD HH.MM.SS Meeting Title"
        folder_name = folder.name
        meeting_date = "Unknown"
        meeting_title = folder_name

        date_match = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}\.\d{2}\.\d{2})\s*(.*)", folder_name)
        if date_match:
            meeting_date  = date_match.group(1)
            time_str      = date_match.group(2).replace(".", ":")
            meeting_title = date_match.group(3).strip() or "Zoom Meeting"

        meeting = {
            "id":            folder_name,
            "title":         meeting_title,
            "date":          meeting_date,
            "folder":        str(folder),
            "has_transcript": len(vtt_files) > 0,
            "has_audio":     len(audio_files) > 0,
            "has_chat":      len(txt_files) > 0,
            "vtt_path":      str(vtt_files[0]) if vtt_files else None,
            "chat_path":     str(txt_files[0]) if txt_files else None,
        }
        meetings.append(meeting)

    return meetings


def load_meeting_transcript(meeting: dict) -> tuple[list[dict], str]:
    """
    Load and parse the transcript for a meeting.
    Returns (segments, full_text)
    """
    if not meeting.get("vtt_path"):
        return [], ""

    vtt_path = Path(meeting["vtt_path"])
    if not vtt_path.exists():
        return [], ""

    segments = parse_vtt(vtt_path)
    text     = vtt_to_transcript_text(segments)
    return segments, text


def load_chat_log(meeting: dict) -> str:
    """Load Zoom chat log for a meeting."""
    if not meeting.get("chat_path"):
        return ""
    chat_path = Path(meeting["chat_path"])
    if not chat_path.exists():
        return ""
    return chat_path.read_text(encoding="utf-8", errors="ignore")


# ── Processed log ─────────────────────────────────────────────────────────────

def load_processed_log() -> set:
    """Load set of already-processed meeting IDs."""
    if not PROCESSED_LOG.exists():
        return set()
    try:
        return set(json.loads(PROCESSED_LOG.read_text()))
    except Exception:
        return set()


def mark_processed(meeting_id: str):
    """Mark a meeting as processed."""
    PROCESSED_LOG.parent.mkdir(parents=True, exist_ok=True)
    processed = load_processed_log()
    processed.add(meeting_id)
    PROCESSED_LOG.write_text(json.dumps(list(processed)))


def get_new_meetings() -> list[dict]:
    """Return meetings that haven't been processed yet."""
    all_meetings = scan_zoom_folder()
    processed    = load_processed_log()
    return [m for m in all_meetings
            if m["has_transcript"] and m["id"] not in processed]


# ── Meetings cache ────────────────────────────────────────────────────────────

def save_meetings_cache(meetings: list[dict]):
    """Save processed meetings with analysis to cache."""
    MEETINGS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MEETINGS_CACHE.write_text(json.dumps(meetings, indent=2))


def load_meetings_cache() -> list[dict]:
    """Load cached processed meetings."""
    if not MEETINGS_CACHE.exists():
        return []
    try:
        return json.loads(MEETINGS_CACHE.read_text())
    except Exception:
        return []


# ── File watcher (background) ─────────────────────────────────────────────────

def watch_zoom_folder(callback=None, poll_interval: int = 60):
    """
    Poll ~/Documents/Zoom every N seconds for new meetings.
    Calls callback(meeting) when new transcript found.
    """
    print(f"👀 Watching {ZOOM_DIR} for new Zoom meetings...")
    print(f"   Polling every {poll_interval}s\n")

    while True:
        new = get_new_meetings()
        if new:
            print(f"📋 Found {len(new)} new meeting(s)!")
            for meeting in new:
                print(f"   → {meeting['title']} ({meeting['date']})")
                if callback:
                    callback(meeting)
        time.sleep(poll_interval)


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║   Zoom Watcher — Transcript Scanner      ║")
    print("╚══════════════════════════════════════════╝\n")

    print(f"📁 Scanning: {ZOOM_DIR}\n")

    meetings = scan_zoom_folder()

    if not meetings:
        print("No Zoom meetings found.")
        print(f"\nMake sure meetings are saved to: {ZOOM_DIR}")
        print("And that cloud recording with transcript is enabled.")
    else:
        print(f"Found {len(meetings)} meeting folder(s):\n")
        for m in meetings:
            status = "✅ has transcript" if m["has_transcript"] else "⚠️  no transcript"
            print(f"  [{status}] {m['title']} ({m['date']})")

        # Test parse first transcript
        with_transcript = [m for m in meetings if m["has_transcript"]]
        if with_transcript:
            print(f"\n── Parsing transcript: {with_transcript[0]['title']} ──")
            segs, text = load_meeting_transcript(with_transcript[0])
            print(f"   Segments: {len(segs)}")
            print(f"   Preview:\n{text[:500]}...")
