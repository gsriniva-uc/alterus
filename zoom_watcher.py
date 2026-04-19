#!/usr/bin/env python3
"""
Alterus Zoom Watcher
Watches ~/Documents/Zoom for new transcripts and sends to Alterus API.

Usage:
  python3 zoom_watcher.py --email your@email.com

Or add to ~/.zshrc to auto-start:
  alias alterus-zoom="python3 ~/ganesh-agent/zoom_watcher.py --email your@email.com &"
"""

import os
import sys
import json
import time
import hashlib
import argparse
import requests
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
ALTERUS_API  = "https://alterus.onrender.com"
ZOOM_DIR     = Path.home() / "Documents" / "Zoom"
SEEN_FILE    = Path.home() / ".alterus_zoom_seen.json"
POLL_SECONDS = 30

def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(list(seen)))

def parse_vtt(path):
    """Extract plain text from a .vtt transcript file."""
    lines = []
    for line in path.read_text(errors='ignore').splitlines():
        line = line.strip()
        if not line or line.startswith('WEBVTT') or '-->' in line or line.isdigit():
            continue
        # Remove speaker labels like "John Smith: "
        if ':' in line and len(line.split(':')[0]) < 30:
            line = ':'.join(line.split(':')[1:]).strip()
        if line:
            lines.append(line)
    return '\n'.join(lines)

def parse_txt(path):
    return path.read_text(errors='ignore')

def find_transcripts():
    """Find all .vtt and .txt transcript files in ~/Documents/Zoom."""
    transcripts = []
    if not ZOOM_DIR.exists():
        return transcripts

    for meeting_dir in ZOOM_DIR.iterdir():
        if not meeting_dir.is_dir():
            continue
        for f in meeting_dir.iterdir():
            if f.suffix in ('.vtt', '.txt') and 'transcript' in f.name.lower():
                transcripts.append((meeting_dir.name, f))
            elif f.suffix == '.vtt':
                transcripts.append((meeting_dir.name, f))

    return transcripts

def send_transcript(user_email, meeting_title, meeting_date, transcript_text):
    """Send transcript to Alterus API for analysis."""
    try:
        res = requests.post(
            f"{ALTERUS_API}/api/zoom/ingest",
            json={
                "transcript":  transcript_text,
                "title":       meeting_title,
                "date":        meeting_date,
                "user_email":  user_email,
            },
            timeout=60
        )
        data = res.json()
        if data.get("success"):
            print(f"  ✅ Analyzed: {meeting_title}")
        else:
            print(f"  ❌ Error: {data.get('error')}")
        return data.get("success", False)
    except Exception as e:
        print(f"  ❌ Failed to send: {e}")
        return False

def watch(user_email):
    seen = load_seen()
    print(f"✦ Alterus Zoom Watcher started")
    print(f"  Watching: {ZOOM_DIR}")
    print(f"  User: {user_email}")
    print(f"  Polling every {POLL_SECONDS}s — Ctrl+C to stop\n")

    while True:
        transcripts = find_transcripts()
        new_count = 0

        for meeting_title, transcript_file in transcripts:
            file_id = str(transcript_file)
            if file_id in seen:
                continue

            print(f"📄 New transcript found: {transcript_file.name}")
            print(f"   Meeting: {meeting_title}")

            # Parse transcript
            if transcript_file.suffix == '.vtt':
                text = parse_vtt(transcript_file)
            else:
                text = parse_txt(transcript_file)

            if len(text) < 50:
                print("   Skipping (too short)")
                seen.add(file_id)
                continue

            # Extract date from folder name (format: YYYY-MM-DD HH.MM.SS Meeting Name)
            parts = meeting_title.split(' ')
            date_str = parts[0] if parts and len(parts[0]) == 10 else datetime.now().strftime('%Y-%m-%d')
            clean_title = ' '.join(parts[2:]) if len(parts) > 2 else meeting_title

            success = send_transcript(user_email, clean_title, date_str, text)
            if success:
                seen.add(file_id)
                new_count += 1
                save_seen(seen)

        if new_count == 0:
            sys.stdout.write(f"\r⏳ Watching... Last check: {datetime.now().strftime('%H:%M:%S')}")
            sys.stdout.flush()
        else:
            print(f"\n✅ Processed {new_count} new meeting(s)\n")

        time.sleep(POLL_SECONDS)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Alterus Zoom Watcher')
    parser.add_argument('--email', required=True, help='Your email address')
    args = parser.parse_args()
    try:
        watch(args.email)
    except KeyboardInterrupt:
        print("\n\nStopped.")
