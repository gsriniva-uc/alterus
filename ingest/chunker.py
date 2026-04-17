"""
chunker.py
Splits scraped conversation .txt files into overlapping chunks
suitable for embedding and retrieval.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tiktoken

# ── Config ────────────────────────────────────────────────────────────────────
CHUNK_SIZE    = 500    # tokens per chunk
CHUNK_OVERLAP = 75     # overlap between chunks to preserve context
ENCODING      = "cl100k_base"  # works for both Claude and OpenAI embeddings


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    text:       str
    source:     str          # original filename
    title:      str          # conversation title
    chunk_idx:  int          # position within document
    token_count: int
    doc_type:   str = "unknown"   # prd / email / strategy / onboarding / etc.
    metadata:   dict = field(default_factory=dict)


# ── Doc type classifier ───────────────────────────────────────────────────────

DOC_TYPE_KEYWORDS = {
    "prd":         ["prd", "product requirement", "requirements doc", "user story",
                    "acceptance criteria", "non-goal", "success metric"],
    "strategy":    ["strategy", "roadmap", "initiative", "okr", "north star",
                    "halliburton", "adoption", "go-to-market", "gtm"],
    "email":       ["email", "subject:", "dear ", "regards", "follow up",
                    "reach out", "outreach"],
    "presentation":["presentation", "slide", "deck", "agenda", "q1", "q2",
                    "q3", "q4", "quarterly"],
    "onboarding":  ["onboarding", "30-60-90", "ramp", "first week", "new hire"],
    "technical":   ["langgraph", "langchain", "python", "api", "agent",
                    "architecture", "databricks", "kafka", "vector"],
    "resume":      ["resume", "ats", "linkedin", "job description", "experience"],
}

def classify_doc_type(title: str, text: str) -> str:
    combined = (title + " " + text[:500]).lower()
    scores   = {}
    for doc_type, keywords in DOC_TYPE_KEYWORDS.items():
        scores[doc_type] = sum(1 for kw in keywords if kw in combined)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


# ── Chunker ───────────────────────────────────────────────────────────────────

class Chunker:
    def __init__(self):
        self.enc = tiktoken.get_encoding(ENCODING)

    def count_tokens(self, text: str) -> int:
        return len(self.enc.encode(text))

    def chunk_text(self, text: str, title: str, source: str) -> list[Chunk]:
        """Split text into overlapping token chunks."""
        doc_type = classify_doc_type(title, text)
        tokens   = self.enc.encode(text)
        chunks   = []
        start    = 0
        idx      = 0

        while start < len(tokens):
            end        = min(start + CHUNK_SIZE, len(tokens))
            chunk_toks = tokens[start:end]
            chunk_text = self.enc.decode(chunk_toks)

            chunks.append(Chunk(
                text        = chunk_text,
                source      = source,
                title       = title,
                chunk_idx   = idx,
                token_count = len(chunk_toks),
                doc_type    = doc_type,
                metadata    = {
                    "source":    source,
                    "title":     title,
                    "doc_type":  doc_type,
                    "chunk_idx": idx,
                    "total_tokens": len(tokens),
                }
            ))

            start += CHUNK_SIZE - CHUNK_OVERLAP
            idx   += 1

        return chunks

    def chunk_file(self, filepath: Path) -> list[Chunk]:
        """Read a .txt conversation file and chunk it."""
        text  = filepath.read_text(encoding="utf-8")
        title = extract_title(text) or filepath.stem
        return self.chunk_text(text, title, filepath.name)

    def chunk_directory(self, directory: Path) -> list[Chunk]:
        """Chunk all .txt files in a directory."""
        all_chunks = []
        txt_files  = sorted(directory.glob("*.txt"))

        print(f"📄 Chunking {len(txt_files)} files...")
        for fp in txt_files:
            chunks = self.chunk_file(fp)
            all_chunks.extend(chunks)
            print(f"   {fp.name}: {len(chunks)} chunks")

        print(f"\n✅ Total chunks: {len(all_chunks)}\n")
        return all_chunks


def extract_title(text: str) -> Optional[str]:
    """Extract TITLE: line from conversation file header."""
    match = re.search(r"^TITLE:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    data_dir = Path("data/claude_conversations")
    chunker  = Chunker()
    chunks   = chunker.chunk_directory(data_dir)

    # Show sample
    print("── Sample chunk ──────────────────────────────")
    sample = chunks[0]
    print(f"Title:    {sample.title}")
    print(f"Doc type: {sample.doc_type}")
    print(f"Tokens:   {sample.token_count}")
    print(f"Text:     {sample.text[:200]}...")
