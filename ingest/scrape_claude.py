"""
Claude.ai Conversation Scraper
Extracts all conversations from claude.ai and saves them as structured text files.
Uses your real Chrome browser to bypass Cloudflare bot detection.

BEFORE RUNNING:
    1. Quit Chrome completely: osascript -e 'quit app "Google Chrome"'
    2. Run: python ingest/scrape_claude.py
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("../data/claude_conversations")
CLAUDE_URL  = "https://claude.ai"
DELAY_MS    = 1200   # ms between requests — be polite to the server
MAX_CONVOS  = None   # set to e.g. 50 to limit; None = scrape everything

# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 60) -> str:
    """Convert a conversation title to a safe filename."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "_", text).strip("_")
    return text[:max_len]


def save_conversation(convo: dict, output_dir: Path) -> Path:
    """Save a single conversation as a JSON + plain-text file."""
    output_dir.mkdir(parents=True, exist_ok=True)

    slug      = slugify(convo.get("title", "untitled"))
    timestamp = convo.get("scraped_at", datetime.now().isoformat())[:10]
    base_name = f"{timestamp}_{slug}"

    # ── JSON (full structured data) ──
    json_path = output_dir / f"{base_name}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(convo, f, indent=2, ensure_ascii=False)

    # ── Plain text (for embedding) ──
    txt_path = output_dir / f"{base_name}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"TITLE: {convo.get('title', 'Untitled')}\n")
        f.write(f"DATE:  {convo.get('scraped_at', '')}\n")
        f.write(f"URL:   {convo.get('url', '')}\n")
        f.write("=" * 80 + "\n\n")
        for msg in convo.get("messages", []):
            role   = msg["role"].upper()
            prefix = "YOU" if role == "HUMAN" else "CLAUDE"
            f.write(f"[{prefix}]\n{msg['content']}\n\n{'─' * 40}\n\n")

    return txt_path


# ── Core scraper ──────────────────────────────────────────────────────────────

CHROME_PATH    = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE = os.path.expanduser("~/Library/Application Support/Google/Chrome")


async def scrape_all():
    """
    Launch real Chrome with your existing profile (bypasses Cloudflare),
    then iterate every conversation and save it.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n📁 Saving conversations to: {OUTPUT_DIR.resolve()}\n")

    async with async_playwright() as pw:

        print("🌐 Launching your Chrome browser...")
        print("⚠️  Make sure Chrome is fully closed before this runs!\n")

        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=CHROME_PROFILE,
                executable_path=CHROME_PATH,
                headless=False,
                args=[
                    "--profile-directory=Default",
                    "--disable-blink-features=AutomationControlled",
                ],
                ignore_default_args=["--enable-automation"],
            )
        except Exception as e:
            print(f"❌ Could not launch Chrome: {e}")
            print("\n💡 Make sure Chrome is fully closed and try again:")
            print("   osascript -e 'quit app \"Google Chrome\"'")
            return

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print("🌐 Opening claude.ai...")
        await page.goto(CLAUDE_URL, wait_until="domcontentloaded")

        # Handle Cloudflare if it appears
        if "challenge" in page.url or "Just a moment" in await page.title():
            print("🔒 Cloudflare check — please click 'Verify you are human' in the browser")
            print("⏳ Waiting up to 30 seconds...")
            try:
                await page.wait_for_url("**/chat**", timeout=30_000)
            except PlaywrightTimeout:
                pass

        print("⏳ Waiting for Claude to load (up to 60 seconds)...")
        print("   If not logged in, please log in manually in the browser.\n")

        try:
            await page.wait_for_selector('a[href*="/chat/"]', timeout=60_000)
            print("✅ Claude loaded successfully!\n")
        except PlaywrightTimeout:
            print("❌ Could not detect conversations. Check the browser window.")
            await ctx.close()
            return

        # ── Collect all conversation links ────────────────────────────────────
        print("📋 Collecting conversation list...")
        await scroll_sidebar_to_load_all(page)

        links = await page.eval_on_selector_all(
            'a[href*="/chat/"]',
            "els => els.map(el => ({ href: el.href, title: el.innerText.trim() }))"
        )

        seen, unique_links = set(), []
        for lnk in links:
            if lnk["href"] not in seen and "/chat/" in lnk["href"]:
                seen.add(lnk["href"])
                unique_links.append(lnk)

        total = len(unique_links)
        if MAX_CONVOS:
            unique_links = unique_links[:MAX_CONVOS]

        print(f"🔍 Found {total} conversations"
              f"{f' (scraping first {MAX_CONVOS})' if MAX_CONVOS else ''}\n")

        # ── Scrape each conversation ──────────────────────────────────────────
        scraped, skipped = 0, 0

        for idx, lnk in enumerate(unique_links, 1):
            title = lnk["title"] or f"conversation_{idx}"
            url   = lnk["href"]

            slug     = slugify(title)
            existing = list(OUTPUT_DIR.glob(f"*_{slug}.txt"))
            if existing:
                print(f"  ⏭  [{idx}/{len(unique_links)}] Skipping (exists): {title[:60]}")
                skipped += 1
                continue

            print(f"  📥 [{idx}/{len(unique_links)}] Scraping: {title[:60]}")

            try:
                convo = await scrape_single_conversation(page, url, title)
                if convo and convo.get("messages"):
                    save_conversation(convo, OUTPUT_DIR)
                    scraped += 1
                    print(f"      ✅ {len(convo['messages'])} messages saved")
                else:
                    print(f"      ⚠️  Empty — skipped")
                    skipped += 1
            except Exception as e:
                print(f"      ❌ Error: {e}")
                skipped += 1

            await asyncio.sleep(DELAY_MS / 1000)

        print(f"\n{'═' * 60}")
        print(f"✅ Done!  Scraped: {scraped}  |  Skipped: {skipped}  |  Total: {total}")
        print(f"📁 Files saved to: {OUTPUT_DIR.resolve()}")
        print(f"{'═' * 60}\n")

        await ctx.close()


async def scroll_sidebar_to_load_all(page, max_scrolls: int = 30):
    """Scroll the sidebar to lazy-load older conversations."""
    sidebar_sel = 'nav, [class*="sidebar"], [class*="Sidebar"]'
    try:
        sidebar = await page.query_selector(sidebar_sel)
        if not sidebar:
            return
        for _ in range(max_scrolls):
            prev_count = len(await page.query_selector_all('a[href*="/chat/"]'))
            await sidebar.evaluate("el => el.scrollTop += 800")
            await asyncio.sleep(0.5)
            new_count = len(await page.query_selector_all('a[href*="/chat/"]'))
            if new_count == prev_count:
                break   # nothing new loaded
    except Exception:
        pass  # sidebar scroll is best-effort


async def scrape_single_conversation(page, url: str, title: str) -> dict:
    """Navigate to a conversation and extract all messages."""
    await page.goto(url, wait_until="domcontentloaded")

    # Wait for messages to render
    try:
        await page.wait_for_selector(
            '[class*="message"], [data-testid*="message"], .font-claude-message',
            timeout=15_000
        )
    except PlaywrightTimeout:
        return {}

    # Scroll to top to make sure all messages loaded
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.8)

    # Extract messages
    messages = await page.evaluate("""
        () => {
            const results = [];

            // Strategy 1: data-testid attributes (most reliable)
            const humanMsgs = document.querySelectorAll('[data-testid="user-message"]');
            const asstMsgs  = document.querySelectorAll('[data-testid="assistant-message"]');

            if (humanMsgs.length > 0 || asstMsgs.length > 0) {
                // Interleave by DOM order
                const allMsgs = [...document.querySelectorAll(
                    '[data-testid="user-message"], [data-testid="assistant-message"]'
                )];
                allMsgs.forEach(el => {
                    results.push({
                        role: el.dataset.testid === 'user-message' ? 'human' : 'assistant',
                        content: el.innerText.trim()
                    });
                });
                return results;
            }

            // Strategy 2: class-based fallback
            const containers = document.querySelectorAll(
                '[class*="ConversationItem"], [class*="message-block"], .font-claude-message'
            );
            containers.forEach(el => {
                const isHuman = el.className.includes('human')
                             || el.className.includes('user')
                             || el.closest('[class*="human"]');
                results.push({
                    role: isHuman ? 'human' : 'assistant',
                    content: el.innerText.trim()
                });
            });

            return results;
        }
    """)

    # Filter empty messages
    messages = [m for m in messages if m.get("content", "").strip()]

    return {
        "title":      title,
        "url":        url,
        "scraped_at": datetime.now().isoformat(),
        "messages":   messages
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║   Claude.ai Conversation Scraper         ║")
    print("║   Building Ganesh Agent Corpus           ║")
    print("╚══════════════════════════════════════════╝\n")
    print("⚠️  PRE-CHECK: Chrome must be fully closed.")
    print("   If not, run: osascript -e 'quit app \"Google Chrome\"'\n")
    input("   Press ENTER when Chrome is closed to continue...")
    asyncio.run(scrape_all())
