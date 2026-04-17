"""
scrape_outlook.py  v4 — Search-based scraper
Searches Outlook Web for emails from each key stakeholder.
No noise, no calendar invites, no junk — only real conversations.

Run:
    python -m ingest.scrape_outlook
"""

import asyncio
import json
import re
import sys
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_TXT  = Path("data/outlook_history.txt")
OUTPUT_JSON = Path("data/outlook_raw.json")
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MAX_PER_PERSON = 20    # emails per stakeholder
DELAY          = 1.2   # seconds between emails

# ── Your key stakeholders ─────────────────────────────────────────────────────
# Format: ("Display Name", "search query for Outlook search bar")
STAKEHOLDERS = [
    ("Jason Wong",    "from:jason.wong"),
    ("Raghu",         "from:raghu"),
    ("Sibanjan Das",  "from:sibanjan.das"),
    ("Jerry Jiang",   "from:jerry.jiang"),
    ("Senthil V",     "from:senthil.v"),
    ("Luke H",        "from:luke"),
    ("Shariq",        "from:shariq"),
    ("Ashraf",        "from:ashraf"),
    # Also search your sent items to each person
    ("Sent to Jason", "to:jason.wong"),
    ("Sent to Raghu", "to:raghu"),
]

# ── Main ──────────────────────────────────────────────────────────────────────

async def scrape_outlook():
    OUTPUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="outlook_search_")

    async with async_playwright() as pw:
        print("🌐 Launching Chrome...")
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir   = tmp,
            executable_path = CHROME_PATH,
            headless        = False,
            args            = ["--no-first-run", "--disable-sync",
                               "--disable-blink-features=AutomationControlled"],
            ignore_default_args = ["--enable-automation"],
            viewport        = {"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # ── Step 1: Manual login ──────────────────────────────────────────────
        await page.goto("https://outlook.office.com/mail",
                        wait_until="domcontentloaded", timeout=30_000)

        print("\n" + "="*60)
        print("👉 Log in to Outlook in the Chrome window")
        print("   Complete MFA if needed")
        print("   Wait until you see your inbox")
        print("="*60)
        input("\nPress ENTER once you can see your inbox...\n")

        all_emails = []

        # ── Step 2: Search for each stakeholder ───────────────────────────────
        for name, query in STAKEHOLDERS:
            print(f"\n🔍 Searching: {query} ({name})")
            emails = await search_and_extract(page, query, name, MAX_PER_PERSON)
            all_emails.extend(emails)
            print(f"   ✅ Found {len(emails)} emails from/to {name}")

        await ctx.close()
        shutil.rmtree(tmp, ignore_errors=True)

    # ── Save ──────────────────────────────────────────────────────────────────
    if all_emails:
        save_emails(all_emails)
        print(f"\n{'='*60}")
        print(f"✅ Total emails saved: {len(all_emails)}")
        print(f"📁 Corpus: {OUTPUT_TXT}")
        print(f"📁 Backup: {OUTPUT_JSON}")
        print(f"\n💡 Now run: python -m ingest.ingest_history")
        print(f"{'='*60}")
    else:
        print("\n⚠️  No emails saved — check Chrome window for errors")


async def search_and_extract(page, query: str, label: str,
                              max_count: int) -> list[dict]:
    """Search Outlook for a query and extract matching emails."""
    emails = []

    try:
        # ── Navigate to search ────────────────────────────────────────────────
        # Use Outlook search URL directly
        search_url = f"https://outlook.office.com/mail/search/{query.replace(':','%3A').replace(' ','+')}"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)

        # If URL approach didn't work, try the search box
        if "search" not in page.url:
            await use_search_box(page, query)

        # Wait for results
        print(f"   ⏳ Waiting for search results...")
        loaded = False
        for sel in ['[data-convid]', '[role="option"]', '[role="listitem"]']:
            try:
                await page.wait_for_selector(sel, timeout=15_000)
                loaded = True
                break
            except PlaywrightTimeout:
                continue

        if not loaded:
            # Try search box as fallback
            print(f"   ↩️  Trying search box...")
            await use_search_box(page, query)
            await asyncio.sleep(3)
            for sel in ['[data-convid]', '[role="option"]']:
                try:
                    await page.wait_for_selector(sel, timeout=10_000)
                    loaded = True
                    break
                except PlaywrightTimeout:
                    continue

        if not loaded:
            print(f"   ⚠️  No results found for: {query}")
            return emails

        # ── Scroll to load more results ───────────────────────────────────────
        await scroll_results(page, max_count)

        # ── Get result rows ───────────────────────────────────────────────────
        rows = []
        for sel in ['[data-convid]', '[role="option"]', '[role="listitem"]']:
            found = await page.query_selector_all(sel)
            if len(found) > len(rows):
                rows = found

        print(f"   📋 Found {len(rows)} results")

        # ── Extract each email ────────────────────────────────────────────────
        for i, row in enumerate(rows[:max_count]):
            try:
                print(f"   [{i+1}/{min(len(rows),max_count)}] Reading...", end="\r")
                await row.scroll_into_view_if_needed()
                await row.click(timeout=5000)
                await asyncio.sleep(DELAY)

                email = await extract_email(page, label)
                if email and len(email.get("body","").split()) >= 10:
                    emails.append(email)
                    print(f"   [{i+1}] ✅ {email.get('subject','')[:55]}", end="\r")

            except Exception:
                continue

        print(f"\n   📬 Extracted {len(emails)} real emails")

    except Exception as e:
        print(f"   ❌ Search error for {query}: {e}")

    return emails


async def use_search_box(page, query: str):
    """Type query into Outlook search box."""
    try:
        # Click search box
        for sel in [
            '[aria-label="Search"]',
            'input[placeholder*="Search"]',
            'input[type="search"]',
            '[role="search"] input',
            '#topSearchInput',
        ]:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                await asyncio.sleep(0.5)
                await el.fill(query)
                await asyncio.sleep(0.5)
                await page.keyboard.press("Enter")
                await asyncio.sleep(2)
                return
    except Exception as e:
        print(f"   ⚠️  Search box error: {e}")


async def scroll_results(page, target: int, max_scrolls: int = 20):
    """Scroll search results to load more."""
    for i in range(max_scrolls):
        prev_count = len(await page.query_selector_all('[data-convid]'))

        await page.evaluate("""
            () => {
                const containers = [
                    ...document.querySelectorAll('[role="list"]'),
                    ...document.querySelectorAll('[role="listbox"]'),
                    ...document.querySelectorAll('div[class*="scroll"]'),
                ];
                for (const el of containers) {
                    if (el.scrollHeight > el.clientHeight + 50) {
                        el.scrollTop += 2000;
                        return;
                    }
                }
                window.scrollBy(0, 2000);
            }
        """)
        await asyncio.sleep(1)

        curr_count = len(await page.query_selector_all('[data-convid]'))
        if curr_count >= target or curr_count == prev_count:
            break


async def extract_email(page, label: str) -> dict:
    """Extract content from the currently open email."""
    await asyncio.sleep(0.3)

    result = await page.evaluate("""
        () => {
            // Subject
            let subject = '';
            for (const sel of [
                '[data-testid="subject"]',
                '.allowTextSelection h1',
                'h1', 'h2',
                '[class*="subject"]',
            ]) {
                const el = document.querySelector(sel);
                if (el && el.innerText.trim().length > 0) {
                    subject = el.innerText.trim().slice(0, 200);
                    break;
                }
            }

            // From
            let from_field = '';
            for (const sel of [
                '[data-testid="from"]',
                '[aria-label*="From"]',
                '[class*="sender"]',
                '[class*="from"]',
            ]) {
                const el = document.querySelector(sel);
                if (el && el.innerText.trim()) {
                    from_field = el.innerText.trim().slice(0, 150);
                    break;
                }
            }

            // Body
            let body = '';
            for (const sel of [
                '[data-testid="body"]',
                '.allowTextSelection',
                '[aria-label="Message body"]',
                '[class*="body"]',
                '[contenteditable="false"]',
                '.x_WordSection1',
            ]) {
                const el = document.querySelector(sel);
                if (el && el.innerText && el.innerText.trim().length > 30) {
                    body = el.innerText.trim();
                    break;
                }
            }

            // Date
            let date = '';
            const timeEl = document.querySelector('time, [datetime]');
            if (timeEl) {
                date = timeEl.getAttribute('datetime') || timeEl.innerText || '';
            }

            return { subject, from_field, body: body.slice(0, 1500), date };
        }
    """)

    body = result.get("body", "").strip()
    if not body:
        return {}

    # Clean reply chains — keep only the top part
    body = re.sub(r"\n(From|Sent|To|Subject):\s.+", "", body, flags=re.DOTALL)
    body = re.sub(r"\n_{5,}.*", "", body, flags=re.DOTALL)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    return {
        "label":      label,
        "subject":    result.get("subject", ""),
        "from_field": result.get("from_field", ""),
        "body":       body,
        "date":       result.get("date", ""),
        "scraped_at": datetime.now().isoformat(),
    }


def save_emails(emails: list[dict]):
    """Save emails as corpus text + JSON backup."""
    lines = [
        "TITLE: Outlook Email History — Ganesh Srinivasan",
        f"DATE: {datetime.now().isoformat()}",
        f"TOTAL: {len(emails)} emails",
        "=" * 80, "",
    ]

    # Group by label
    by_label = {}
    for e in emails:
        lbl = e.get("label", "Other")
        by_label.setdefault(lbl, []).append(e)

    for label, group in by_label.items():
        lines.append(f"\n[{label.upper()} — {len(group)} emails]\n")
        for e in group:
            lines += [
                f"Subject: {e.get('subject','')}",
                f"From: {e.get('from_field','')}",
                f"Date: {e.get('date','')}",
                "",
                e.get("body", ""),
                "",
                "─" * 60,
                "",
            ]

    OUTPUT_TXT.write_text("\n".join(lines), encoding="utf-8")
    OUTPUT_JSON.write_text(
        json.dumps(emails, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║   Outlook Scraper v4 — Search Based      ║")
    print("╚══════════════════════════════════════════╝\n")
    print("Searches for emails from each key stakeholder.")
    print("No junk, no calendar invites — only real conversations.\n")
    print("⚠️  Close Chrome first:")
    print("   osascript -e 'quit app \"Google Chrome\"'\n")
    input("Press ENTER when Chrome is closed...")
    asyncio.run(scrape_outlook())
