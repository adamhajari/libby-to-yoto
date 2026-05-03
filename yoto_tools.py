"""yoto_tools.py — Utilities for interacting with the Yoto web app."""

import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext

from libby_to_yoto import inject_cookies, YOTO_PLAYLISTS


WORKSPACE = Path.home() / ".libby_to_yoto"
CHROMIUM_PROFILE = WORKSPACE / "chromium-profile"


async def make_context(pw) -> BrowserContext:
    CHROMIUM_PROFILE.mkdir(parents=True, exist_ok=True)
    ctx = await pw.chromium.launch_persistent_context(
        str(CHROMIUM_PROFILE),
        headless=False,
        args=[
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
        no_viewport=True,
        ignore_https_errors=True,
    )
    await ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    """)
    await inject_cookies(ctx, "my.yotoplay.com")
    return ctx


async def cmd_list_playlists():
    async with async_playwright() as pw:
        ctx = await make_context(pw)
        page = await ctx.new_page()

        print(f"Opening {YOTO_PLAYLISTS}…")
        await page.goto(YOTO_PLAYLISTS, wait_until="networkidle", timeout=30000)

        if "login" in page.url.lower() or "sign-in" in page.url.lower():
            print("⚠ Not logged in — please log in in the browser window.")
            await asyncio.get_event_loop().run_in_executor(None, input, "Press ENTER after logging in… ")
            await page.goto(YOTO_PLAYLISTS, wait_until="networkidle", timeout=30000)

        # Playlist titles are rendered as heading text inside each playlist card.
        # Try h2/h3 first; fall back to any element with a playlist-card role/class.
        titles = await page.evaluate("""() => {
            const selectors = ['h2', 'h3', '[class*="playlist"] [class*="title"]', '[class*="card"] [class*="title"]'];
            for (const sel of selectors) {
                const els = [...document.querySelectorAll(sel)];
                const texts = els.map(e => e.textContent.trim()).filter(t => t.length > 0);
                if (texts.length > 0) return texts;
            }
            return [];
        }""")

        await page.close()
        await ctx.close()

    if titles:
        print(f"\n{len(titles)} playlist(s):\n")
        for t in titles:
            print(f"  {t}")
    else:
        print("No playlists found (or page structure has changed).")


async def main():
    parser = argparse.ArgumentParser(description="Yoto web app tools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-playlists", help="Print titles of all Yoto playlists")
    args = parser.parse_args()

    if args.command == "list-playlists":
        await cmd_list_playlists()


def main_sync():
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
