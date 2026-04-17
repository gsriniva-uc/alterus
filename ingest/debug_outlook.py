"""
debug_outlook.py
Opens Outlook Web and inspects the DOM to find the right
scroll container and email selectors.

Run:
    python -m ingest.debug_outlook
"""

import asyncio
import os
import shutil
import tempfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright

CHROME_PATH    = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Profile 1"
)
SENT_URL = "https://outlook.office.com/mail/sentitems"


def copy_profile() -> str:
    tmp = tempfile.mkdtemp(prefix="debug_outlook_")
    tmp_default = os.path.join(tmp, "Default")
    os.makedirs(tmp_default, exist_ok=True)
    for item in ["Cookies","Cookies-journal","Local Storage",
                 "Session Storage","IndexedDB","Web Data","Preferences"]:
        src = os.path.join(CHROME_PROFILE, item)
        dst = os.path.join(tmp_default, item)
        try:
            if os.path.isfile(src): shutil.copy2(src, dst)
            elif os.path.isdir(src): shutil.copytree(src, dst, dirs_exist_ok=True)
        except: pass
    return tmp


async def debug():
    tmp = copy_profile()
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir   = tmp,
            executable_path = CHROME_PATH,
            headless        = False,
            args            = ["--disable-blink-features=AutomationControlled",
                               "--no-first-run","--disable-sync"],
            ignore_default_args = ["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print("🌐 Navigating to Sent Items...")
        await page.goto(SENT_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(8)

        print(f"📍 URL: {page.url}")
        print(f"📄 Title: {await page.title()}")

        # Dump DOM info to find right selectors
        info = await page.evaluate("""
        () => {
            const result = {
                email_counts: {},
                scroll_containers: [],
                page_text_sample: document.body.innerText.slice(0, 300),
            };

            // Count elements by various selectors
            const selectors = [
                '[data-convid]',
                '[role="option"]',
                '[role="listitem"]',
                '[class*="ms-List-cell"]',
                '[class*="mail-list"]',
                '[class*="listItem"]',
                '[class*="row"]',
                'tr',
                '[aria-label*="mail"]',
            ];

            selectors.forEach(sel => {
                try {
                    result.email_counts[sel] = document.querySelectorAll(sel).length;
                } catch(e) {}
            });

            // Find scrollable containers
            document.querySelectorAll('*').forEach(el => {
                if (el.scrollHeight > el.clientHeight + 50 &&
                    el.clientHeight > 100 &&
                    el.clientHeight < window.innerHeight) {
                    result.scroll_containers.push({
                        tag:      el.tagName,
                        class:    el.className.slice(0,80),
                        role:     el.getAttribute('role') || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        scrollH:  el.scrollHeight,
                        clientH:  el.clientHeight,
                        id:       el.id || '',
                    });
                }
            });

            return result;
        }
        """)

        print("\n── Email element counts ──────────────────")
        for sel, count in info["email_counts"].items():
            if count > 0:
                print(f"  {count:4d}  {sel}")

        print("\n── Scrollable containers ─────────────────")
        for c in info["scroll_containers"][:10]:
            print(f"  {c['tag']} | role={c['role']} | aria={c['ariaLabel'][:40]} | "
                  f"class={c['class'][:50]} | scroll={c['scrollH']} client={c['clientH']}")

        print("\n── Page text sample ──────────────────────")
        print(info["page_text_sample"])

        print("\n⏸  Chrome stays open — check it manually.")
        print("   Press ENTER to close...")
        input()

        await ctx.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("⚠️  Close Chrome first: osascript -e 'quit app \"Google Chrome\"'")
    input("Press ENTER when ready...")
    asyncio.run(debug())
