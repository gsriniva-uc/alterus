"""
ingest_history.py
Ingests your communication history into the Chroma corpus:
  - Outlook emails (from live webhook data already collected)
  - Teams messages (from live webhook data)
  - Zoom transcripts (from processed meetings)
  - PST export (if available)

This is what makes draft responses accurate — the agent can see
what you've said to each person before.

Run:
    python -m ingest.ingest_history
"""

import sys
import json
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest.chunker import Chunker
from ingest.embedder import CorpusStore, check_ollama

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR       = Path("data")
INBOX_FILE     = DATA_DIR / "live_emails.json"
TEAMS_FILE     = DATA_DIR / "live_teams.json"
ZOOM_FILE      = DATA_DIR / "zoom_meetings.json"
OUTLOOK_EXPORT = DATA_DIR / "outlook_history.txt"   # we generate this
TEAMS_EXPORT   = DATA_DIR / "teams_history.txt"     # we generate this
CHROMA_DIR     = DATA_DIR / "chroma_db"


# ── Email history formatter ───────────────────────────────────────────────────

def format_emails_as_text(emails: list[dict]) -> str:
    """Convert email list to text for embedding."""
    lines = []
    for e in emails:
        lines.append(f"EMAIL CONVERSATION")
        lines.append(f"From: {e.get('from', 'Unknown')}")
        lines.append(f"Subject: {e.get('subject', '')}")
        lines.append(f"Date: {e.get('time', '')}")
        lines.append(f"Body: {e.get('body', e.get('preview', ''))}")
        lines.append("─" * 40)
    return "\n".join(lines)


def format_teams_as_text(messages: list[dict]) -> str:
    """Convert Teams messages to text for embedding."""
    lines = []
    for m in messages:
        lines.append(f"TEAMS MESSAGE")
        lines.append(f"From: {m.get('from', 'Unknown')}")
        lines.append(f"Time: {m.get('time', '')}")
        lines.append(f"Message: {m.get('message', '')}")
        lines.append("─" * 40)
    return "\n".join(lines)


def format_zoom_as_text(meetings: list[dict]) -> str:
    """Convert Zoom meeting analyses to text for embedding."""
    lines = []
    for m in meetings:
        if not m.get("summary"):
            continue
        lines.append(f"ZOOM MEETING: {m.get('meeting_title','')}")
        lines.append(f"Date: {m.get('meeting_date','')}")
        lines.append(f"Participants: {', '.join(m.get('participants',[]))}")
        lines.append(f"Summary: {m.get('summary','')}")

        if m.get("action_items"):
            lines.append("Action Items:")
            for item in m["action_items"]:
                lines.append(f"  - [{item.get('owner','?')}] {item.get('action','?')}")

        if m.get("decisions"):
            lines.append("Decisions:")
            for d in m["decisions"]:
                lines.append(f"  - {d.get('decision','?')}")

        if m.get("transcript_preview"):
            lines.append(f"Transcript excerpt: {m['transcript_preview'][:300]}")
        lines.append("─" * 40)
    return "\n".join(lines)


# ── PST parser helper ─────────────────────────────────────────────────────────

def parse_pst_to_text(pst_path: Path) -> str:
    """
    Try to parse a PST file using libpff/pypff.
    Falls back to instructions if library not available.
    """
    try:
        import pypff
        pst = pypff.file()
        pst.open(str(pst_path))
        root = pst.get_root_folder()
        emails = []

        def extract_folder(folder):
            for i in range(folder.get_number_of_sub_messages()):
                try:
                    msg = folder.get_sub_message(i)
                    emails.append({
                        "subject": msg.get_subject() or "",
                        "sender":  msg.get_sender_name() or "",
                        "body":    (msg.get_plain_text_body() or b"").decode("utf-8","ignore")[:500],
                        "date":    str(msg.get_delivery_time() or ""),
                    })
                except Exception:
                    pass
            for i in range(folder.get_number_of_sub_folders()):
                extract_folder(folder.get_sub_folder(i))

        extract_folder(root)
        pst.close()

        lines = []
        for e in emails[:500]:  # limit to 500 emails
            lines.append(f"EMAIL\nFrom: {e['sender']}\nSubject: {e['subject']}\nDate: {e['date']}\nBody: {e['body']}\n---")
        return "\n".join(lines)

    except ImportError:
        print("   ℹ️  pypff not installed — PST parsing unavailable")
        print("   Install with: pip install pypff-python")
        print("   Alternative: export emails as .txt files from Outlook")
        return ""
    except Exception as e:
        print(f"   ⚠️  PST parse error: {e}")
        return ""


# ── Main ingest ───────────────────────────────────────────────────────────────

def ingest_history(force: bool = False):
    """
    Ingest all available communication history into Chroma.
    Sources:
      1. Live webhook emails (already collected by webhook server)
      2. Live webhook Teams messages
      3. Processed Zoom meetings
      4. outlook_history.txt (if exported manually)
      5. teams_history.txt (if exported manually)
    """
    print("╔══════════════════════════════════════════╗")
    print("║   Communication History Ingest           ║")
    print("╚══════════════════════════════════════════╝\n")

    if not check_ollama():
        print("❌ Ollama not running. Start with: ollama serve")
        return

    chunker = Chunker()
    store   = CorpusStore(CHROMA_DIR)
    total_chunks = 0

    # ── 1. Live email history ─────────────────────────────────────────────────
    print("📧 Processing live email history...")
    emails = []
    if INBOX_FILE.exists():
        emails = json.loads(INBOX_FILE.read_text())
    if emails:
        text   = format_emails_as_text(emails)
        fpath  = DATA_DIR / "_email_history_live.txt"
        fpath.write_text(f"TITLE: Live Outlook Email History\nDATE: {datetime.now().isoformat()}\n{'='*80}\n\n{text}")
        chunks = chunker.chunk_file(fpath)
        store.add_chunks(chunks)
        total_chunks += len(chunks)
        print(f"   ✅ {len(emails)} emails → {len(chunks)} chunks")
    else:
        print("   ⚠️  No live emails yet — send some emails to populate")

    # ── 2. Live Teams history ─────────────────────────────────────────────────
    print("\n💬 Processing live Teams history...")
    teams_msgs = []
    if TEAMS_FILE.exists():
        teams_msgs = json.loads(TEAMS_FILE.read_text())
    if teams_msgs:
        text   = format_teams_as_text(teams_msgs)
        fpath  = DATA_DIR / "_teams_history_live.txt"
        fpath.write_text(f"TITLE: Live Teams Message History\nDATE: {datetime.now().isoformat()}\n{'='*80}\n\n{text}")
        chunks = chunker.chunk_file(fpath)
        store.add_chunks(chunks)
        total_chunks += len(chunks)
        print(f"   ✅ {len(teams_msgs)} messages → {len(chunks)} chunks")
    else:
        print("   ⚠️  No Teams messages yet")

    # ── 3. Zoom meeting transcripts ───────────────────────────────────────────
    print("\n🎥 Processing Zoom meeting history...")
    zoom_meetings = []
    if ZOOM_FILE.exists():
        zoom_meetings = json.loads(ZOOM_FILE.read_text())
    if zoom_meetings:
        text  = format_zoom_as_text(zoom_meetings)
        fpath = DATA_DIR / "_zoom_history.txt"
        fpath.write_text(f"TITLE: Zoom Meeting History\nDATE: {datetime.now().isoformat()}\n{'='*80}\n\n{text}")
        chunks = chunker.chunk_file(fpath)
        store.add_chunks(chunks)
        total_chunks += len(chunks)
        print(f"   ✅ {len(zoom_meetings)} meetings → {len(chunks)} chunks")
    else:
        print("   ⚠️  No Zoom meetings processed yet")

    # ── 4. Manual Outlook export (.txt) ──────────────────────────────────────
    print("\n📂 Checking for manual Outlook export...")
    if OUTLOOK_EXPORT.exists():
        chunks = chunker.chunk_file(OUTLOOK_EXPORT)
        store.add_chunks(chunks)
        total_chunks += len(chunks)
        print(f"   ✅ outlook_history.txt → {len(chunks)} chunks")
    else:
        print(f"   ℹ️  No outlook_history.txt found at {OUTLOOK_EXPORT}")
        print("   To add: export Outlook emails as text and save there")

    # ── 5. Manual Teams export (.txt) ────────────────────────────────────────
    print("\n📂 Checking for manual Teams export...")
    if TEAMS_EXPORT.exists():
        chunks = chunker.chunk_file(TEAMS_EXPORT)
        store.add_chunks(chunks)
        total_chunks += len(chunks)
        print(f"   ✅ teams_history.txt → {len(chunks)} chunks")
    else:
        print(f"   ℹ️  No teams_history.txt found at {TEAMS_EXPORT}")

    # ── Summary ───────────────────────────────────────────────────────────────
    stats = store.stats()
    print(f"\n{'═'*50}")
    print(f"✅ History ingest complete")
    print(f"   New chunks added:    {total_chunks}")
    print(f"   Total corpus size:   {stats['total_chunks']} chunks")
    print(f"   Unique documents:    {stats['unique_documents']}")
    print(f"\n💡 The agent will now use this history when drafting responses.")
    print(f"{'═'*50}\n")


# ── Schedule regular re-ingestion ─────────────────────────────────────────────

def schedule_reingestion(interval_minutes: int = 30):
    """Re-ingest live data every N minutes to stay current."""
    import time
    print(f"⏰ Auto re-ingesting every {interval_minutes} minutes...")
    while True:
        ingest_history()
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    import sys
    if "--schedule" in sys.argv:
        schedule_reingestion()
    else:
        ingest_history()
