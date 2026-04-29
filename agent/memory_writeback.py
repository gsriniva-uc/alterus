"""
memory_writeback.py
Writes approved drafts and zoom meetings back to the user's Chroma corpus.

FIX v1.0.1:
  Chunk() constructor does not accept chunk_id as a keyword argument in
  the installed version of chromadb. Pass id as first positional arg or
  use the correct API. Updated to pass id via metadata and let Chroma
  auto-generate the document ID, which is the safe cross-version approach.
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
        from ingest.embedder import CorpusStore

        chroma_dir = Path(os.getenv("DATA_DIR", "data")) / "chroma_db"
        store      = CorpusStore(chroma_dir, user_id=user_email)

        chunk_id = f"draft_{platform}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{abs(hash(draft)) % 10000}"

        # FIX: use store.add_texts() which doesn't require Chunk objects
        # and works across chromadb versions
        added = store.add_texts(
            texts     = [draft],
            metadatas = [{
                "source":    f"{platform}_draft",
                "task_type": task_type,
                "feedback":  feedback,
                "platform":  platform,
                "date":      datetime.now().isoformat(),
                "user":      user_email,
                "context":   context[:200] if context else "",
                "chunk_id":  chunk_id,
            }],
            ids = [chunk_id],
        )
        print(f"✍️  Memory write-back: chunk added for {user_email}")
        return True

    except AttributeError:
        # Fallback: try the Chunk-based approach with positional args
        try:
            from ingest.chunker import Chunk
            from ingest.embedder import CorpusStore

            chroma_dir = Path(os.getenv("DATA_DIR", "data")) / "chroma_db"
            store      = CorpusStore(chroma_dir, user_id=user_email)
            chunk_id   = f"draft_{platform}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{abs(hash(draft)) % 10000}"

            # Try positional — different Chunk signatures across versions
            try:
                chunk = Chunk(chunk_id, draft, {
                    "source":    f"{platform}_draft",
                    "task_type": task_type,
                    "feedback":  feedback,
                    "platform":  platform,
                    "date":      datetime.now().isoformat(),
                    "user":      user_email,
                })
            except TypeError:
                # Last resort: text-only Chunk
                chunk = Chunk(text=draft, metadata={
                    "source":   f"{platform}_draft",
                    "platform": platform,
                    "user":     user_email,
                })

            store.add_chunks([chunk])
            return True
        except Exception as e2:
            print(f"Memory write-back fallback also failed: {e2}")
            return False

    except Exception as e:
        print(f"Memory write-back failed: {e}")
        return False


def write_zoom_to_corpus(user_email: str, meeting: dict) -> bool:
    """Write zoom meeting analysis to corpus for future drafting context."""
    if not meeting:
        return False

    try:
        from ingest.embedder import CorpusStore

        chroma_dir = Path(os.getenv("DATA_DIR", "data")) / "chroma_db"
        store      = CorpusStore(chroma_dir, user_id=user_email or "default")

        texts     = []
        metadatas = []
        ids       = []
        meeting_id = meeting.get("id", datetime.now().strftime("%Y%m%d_%H%M%S"))

        # Summary chunk
        if meeting.get("summary"):
            texts.append(
                f"Meeting: {meeting.get('meeting_title', '')}\n"
                f"Date: {meeting.get('meeting_date', '')}\n"
                f"Summary: {meeting['summary']}"
            )
            metadatas.append({"source": "zoom_meeting", "type": "summary", "user": user_email or "default"})
            ids.append(f"zoom_summary_{meeting_id}")

        # Action item chunks
        for i, item in enumerate(meeting.get("action_items", [])):
            text = (
                f"Action item from {meeting.get('meeting_title', '')}: "
                f"{item.get('owner', '')} will {item.get('action', '')} "
                f"by {item.get('deadline', '')}"
            )
            texts.append(text)
            metadatas.append({"source": "zoom_meeting", "type": "action_item", "user": user_email or "default"})
            ids.append(f"zoom_action_{meeting_id}_{i}")

        if texts:
            store.add_texts(texts=texts, metadatas=metadatas, ids=ids)
            print(f"🎥 Zoom write-back: {len(texts)} chunk(s) added for {user_email or 'default'}")
            return True

        return False

    except AttributeError:
        # Fallback for stores without add_texts
        try:
            from ingest.chunker import Chunk
            from ingest.embedder import CorpusStore

            chroma_dir = Path(os.getenv("DATA_DIR", "data")) / "chroma_db"
            store      = CorpusStore(chroma_dir, user_id=user_email or "default")
            meeting_id = meeting.get("id", datetime.now().strftime("%Y%m%d_%H%M%S"))
            chunks     = []

            if meeting.get("summary"):
                try:
                    chunks.append(Chunk(
                        f"zoom_summary_{meeting_id}",
                        f"Meeting: {meeting.get('meeting_title', '')}\nSummary: {meeting['summary']}",
                        {"source": "zoom_meeting", "type": "summary"},
                    ))
                except TypeError:
                    chunks.append(Chunk(
                        text=f"Meeting: {meeting.get('meeting_title', '')}\nSummary: {meeting['summary']}",
                        metadata={"source": "zoom_meeting"},
                    ))

            if chunks:
                store.add_chunks(chunks)
                return True
        except Exception as e2:
            print(f"Zoom write-back fallback failed: {e2}")
        return False

    except Exception as e:
        print(f"Zoom write-back failed: {e}")
        return False
