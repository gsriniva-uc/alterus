"""
Claude.ai Conversation Scraper v4
Copies Chrome Profile 1 cookies to temp dir, opens Claude, pauses so you
can switch to your PROD workspace, then scrapes all conversations.

BEFORE RUNNING:
    1. Quit Chrome: osascript -e 'quit app "Google Chrome"'
    2. Run: python ingest/scrape_claude.py
"""

import asyncio
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR     = Path("data/claude_conversations")
CLAUDE_URL     = "https://claude.ai"
DELAY_MS       = 1200
MAX_CONVOS     = None   # None = all; set to e.g. 50 to test first

CHROME_PATH    = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE = os.path.expanduser("~/Library/Application Support/Google/Chrome/Profile 1")


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "_", text).strip("_")
    return text[:max_len]


def save_conversation(convo: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug      = slugify(convo.get("title", "untitled"))
    timestamp = convo.get("scraped_at", datetime.now().isoformat())[:10]
    base_name = f"{timestamp}_{slug}"

    json_path = output_dir / f"{base_name}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(convo, f, indent=2, ensure_ascii=False)

    txt_path = output_dir / f"{base_name}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"TITLE: {convo.get('title', 'Untitled')}\n")
        f.write(f"DATE:  {convo.get('scraped_at', '')}\n")
        f.write(f"URL:   {convo.get('url', '')}\n")
        f.write("=" * 80 + "\n\n")
        for msg in convo.get("messages", []):
            prefix = "YOU" if msg["role"] == "human" else "CLAUDE"
            f.write(f"[{prefix}]\n{msg['content']}\n\n{'─' * 40}\n\n")

    return txt_path


def copy_chrome_profile() -> str:
    """Copy essential session files to a temp dir so Playwright can attach."""
    print("📋 Copying Chrome session to temp directory...")
    tmp         = tempfile.mkdtemp(prefix="claude_scraper_")
    tmp_default = os.path.join(tmp, "Default")
    os.makedirs(tmp_default, exist_ok=True)

    essential = [
        "Cookies",
        "Cookies-journal",
        "Local Storage",
        "Session Storage",
        "IndexedDB",
        "Web Data",
        "Preferences",
    ]

    for item in essential:
        src_path = os.path.join(CHROME_PROFILE, item)
        dst_path = os.path.join(tmp_default, item)
        try:
            if os.path.isfile(src_path):
                shutil.copy2(src_path, dst_path)
            elif os.path.isdir(src_path):
                shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
        except Exception as e:
            print(f"   ⚠️  Could not copy {item}: {e}")

    print(f"✅ Profile copied\n")
    return tmp


# ── Core scraper ──────────────────────────────────────────────────────────────

async def scrape_all():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"📁 Output folder: {OUTPUT_DIR.resolve()}\n")

    tmp_profile = copy_chrome_profile()

    async with async_playwright() as pw:

        print("🌐 Launching Chrome...")
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=tmp_profile,
                executable_path=CHROME_PATH,
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-sync",
                ],
                ignore_default_args=["--enable-automation"],
            )
        except Exception as e:
            print(f"❌ Could not launch Chrome: {e}")
            shutil.rmtree(tmp_profile, ignore_errors=True)
            return

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Navigate to Claude
        print("🌐 Navigating to claude.ai...")
        try:
            await page.goto(CLAUDE_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            pass

        await asyncio.sleep(2)

        # Handle Cloudflare
        title = await page.title()
        if "Just a moment" in title or "challenge" in page.url:
            print("🔒 Cloudflare check — click 'Verify you are human' in Chrome")
            try:
                await page.wait_for_function(
                    "() => !document.title.includes('Just a moment')",
                    timeout=30_000
                )
            except PlaywrightTimeout:
                pass

        # ── PAUSE: let user switch to prod workspace ──────────────────────────
        print("=" * 60)
        print("👉 ACTION REQUIRED IN CHROME:")
        print("   1. Look at the bottom-left of Claude for the workspace switcher")
        print("   2. Click it and select your PROD workspace")
        print("   3. Wait for your prod conversations to appear in the sidebar")
        print("=" * 60)
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: input("\n   Press ENTER here once you can see PROD conversations...\n")
        )

        # Wait for conversations to be visible
        print("⏳ Detecting conversations...")
        try:
            await page.wait_for_selector('a[href*="/chat/"]', timeout=30_000)
            print("✅ Conversations detected!\n")
        except PlaywrightTimeout:
            print("❌ No conversations found. Make sure you switched to prod.")
            await ctx.close()
            shutil.rmtree(tmp_profile, ignore_errors=True)
            return

        # Scroll sidebar to load all conversations
        print("📋 Loading full conversation list (scrolling sidebar)...")
        await scroll_sidebar(page)

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
              f"{f' — scraping first {MAX_CONVOS}' if MAX_CONVOS else ''}\n")

        if total == 0:
            print("❌ No conversations found. Did you switch to prod?")
            await ctx.close()
            shutil.rmtree(tmp_profile, ignore_errors=True)
            return

        # Scrape each conversation
        scraped, skipped = 0, 0

        for idx, lnk in enumerate(unique_links, 1):
            title = lnk["title"] or f"conversation_{idx}"
            url   = lnk["href"]

            slug     = slugify(title)
            existing = list(OUTPUT_DIR.glob(f"*_{slug}.txt"))
            if existing:
                print(f"  ⏭  [{idx}/{len(unique_links)}] Skipping: {title[:60]}")
                skipped += 1
                continue

            print(f"  📥 [{idx}/{len(unique_links)}] {title[:60]}")

            try:
                convo = await scrape_conversation(page, url, title)
                if convo and convo.get("messages"):
                    save_conversation(convo, OUTPUT_DIR)
                    scraped += 1
                    print(f"      ✅ {len(convo['messages'])} messages")
                else:
                    print(f"      ⚠️  Empty — skipped")
                    skipped += 1
            except Exception as e:
                print(f"      ❌ {e}")
                skipped += 1

            await asyncio.sleep(DELAY_MS / 1000)

        print(f"\n{'═' * 60}")
        print(f"✅ Done! Scraped: {scraped} | Skipped: {skipped} | Total: {total}")
        print(f"📁 {OUTPUT_DIR.resolve()}")
        print(f"{'═' * 60}\n")

        await ctx.close()
        shutil.rmtree(tmp_profile, ignore_errors=True)
        print("🧹 Temp profile cleaned up.")


async def scroll_sidebar(page, max_scrolls: int = 40):
    try:
        for _ in range(max_scrolls):
            prev = len(await page.query_selector_all('a[href*="/chat/"]'))
            await page.evaluate("""
                const el = document.querySelector('nav')
                        || document.querySelector('[class*="sidebar"]');
                if (el) el.scrollTop += 800;
                else window.scrollBy(0, 800);
            """)
            await asyncio.sleep(0.6)
            curr = len(await page.query_selector_all('a[href*="/chat/"]'))
            if curr == prev:
                break
    except Exception:
        pass


async def scrape_conversation(page, url: str, title: str) -> dict:
    await page.goto(url, wait_until="domcontentloaded", timeout=20_000)

    try:
        await page.wait_for_selector(
            '[data-testid="user-message"], [data-testid="assistant-message"]',
            timeout=15_000
        )
    except PlaywrightTimeout:
        return {}

    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.8)
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.3)

    messages = await page.evaluate("""
        () => {
            const results = [];
            const all = document.querySelectorAll(
                '[data-testid="user-message"], [data-testid="assistant-message"]'
            );
            if (all.length > 0) {
                all.forEach(el => results.push({
                    role: el.dataset.testid === 'user-message' ? 'human' : 'assistant',
                    content: el.innerText.trim()
                }));
                return results;
            }
            // Fallback
            document.querySelectorAll('[class*="message"]').forEach(el => {
                const text = el.innerText.trim();
                if (text.length > 10) {
                    const isHuman = /human|user/i.test(el.className);
                    results.push({ role: isHuman ? 'human' : 'assistant', content: text });
                }
            });
            return results;
        }
    """)

    return {
        "title":      title,
        "url":        url,
        "scraped_at": datetime.now().isoformat(),
        "messages":   [m for m in messages if m.get("content", "").strip()],
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║   Claude.ai Scraper v4                   ║")
    print("║   Building Ganesh Agent Corpus           ║")
    print("╚══════════════════════════════════════════╝\n")
    print("⚠️  Chrome must be fully closed first.")
    print("   Run: osascript -e 'quit app \"Google Chrome\"'\n")
    input("   Press ENTER when Chrome is closed...")
    asyncio.run(scrape_all())
