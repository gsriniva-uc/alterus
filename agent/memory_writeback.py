"""
memory_writeback.py
Writes approved drafts back to the user's corpus.
This improves personalization over time — the more you use Alterus,
the better it knows your voice.
"""

import os
import re
from pathlib import Path
from datetime import datetime


def write_to_corpus(
    user_email: str,
    draft: str,
    context: str,
    platform: str,
    task_type: str = "draft",
    feedback: str = "approved",
) -> bool:
    """
    Write an approved draft to the user's Chroma corpus.
    Called when user clicks thumbs up or copies a draft.
    """
    if not draft or len(draft.strip()) < 20:
        return False

    try:
        from ingest.chunker import Chunk
        from ingest.embedder import CorpusStore

        chroma_dir = Path(os.getenv("DATA_DIR", "data")) / "chroma_db"
        store = CorpusStore(chroma_dir, user_id=user_email)

        # Create a chunk from the draft
        chunk_id = f"draft_{platform}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hash(draft) % 10000}"

        chunk = Chunk(
            chunk_id  = chunk_id,
            text      = draft,
            metadata  = {
                "source":    f"{platform}_draft",
                "task_type": task_type,
                "feedback":  feedback,
                "platform":  platform,
                "date":      datetime.now().isoformat(),
                "user":      user_email,
                "context":   context[:200] if context else "",
            }
        )

        added = store.add_chunks([chunk])
        print(f"✍️ Memory write-back: {added} chunk(s) added for {user_email}")
        return added > 0

    except Exception as e:
        print(f"Memory write-back failed: {e}")
        return False


def write_zoom_to_corpus(user_email: str, meeting: dict) -> bool:
    """Write zoom meeting analysis to corpus for future context."""
    if not meeting:
        return False

    try:
        from ingest.chunker import Chunk
        from ingest.embedder import CorpusStore

        chroma_dir = Path(os.getenv("DATA_DIR", "data")) / "chroma_db"
        store = CorpusStore(chroma_dir, user_id=user_email)

        chunks = []

        # Add summary
        if meeting.get("summary"):
            chunks.append(Chunk(
                chunk_id = f"zoom_summary_{meeting.get('id', '')}",
                text     = f"Meeting: {meeting.get('meeting_title', '')}\nDate: {meeting.get('meeting_date', '')}\nSummary: {meeting['summary']}",
                metadata = {"source": "zoom_meeting", "type": "summary", "user": user_email}
            ))

        # Add action items
        for i, item in enumerate(meeting.get("action_items", [])):
            chunks.append(Chunk(
                chunk_id = f"zoom_action_{meeting.get('id', '')}_{i}",
                text     = f"Action item from {meeting.get('meeting_title', '')}: {item.get('owner', '')} will {item.get('action', '')} by {item.get('deadline', '')}",
                metadata = {"source": "zoom_meeting", "type": "action_item", "user": user_email}
            ))

        if chunks:
            added = store.add_chunks(chunks)
            print(f"🎥 Zoom write-back: {added} chunk(s) added for {user_email}")
            return added > 0

    except Exception as e:
        print(f"Zoom write-back failed: {e}")
        return False
