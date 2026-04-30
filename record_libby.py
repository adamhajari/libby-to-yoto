#!/usr/bin/env python3
"""
record_libby.py — Open Libby with your Chrome cookies and record your actions.

Usage:
    uv run python record_libby.py --output /tmp/libby_recorded.py
"""

import argparse
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
import browser_cookie3


def load_chrome_cookies(domain: str) -> list[dict]:
    jar = browser_cookie3.chrome(domain_name=domain)
    cookies = []
    for c in jar:
        cookie: dict = {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "httpOnly": bool(c.has_nonstandard_attr("HttpOnly")),
            "secure": bool(c.secure),
        }
        if c.expires:
            cookie["expires"] = c.expires
        cookies.append(cookie)
    return cookies


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/libby_recorded.py")
    args = parser.parse_args()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = await browser.new_context(no_viewport=True)
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        """)

        cookies = load_chrome_cookies("libbyapp.com")
        await ctx.add_cookies(cookies)
        print(f"Injected {len(cookies)} Libby cookies")

        page = await ctx.new_page()
        await page.goto("https://libbyapp.com/shelf", wait_until="domcontentloaded")

        print("""
┌─────────────────────────────────────────────────────────────────┐
│  The Playwright Inspector is opening.                            │
│                                                                  │
│  Click the  ⏺ Record  button in the Inspector to start.         │
│                                                                  │
│  Then walk through in the browser:                               │
│    1. Open the audiobook player                                  │
│    2. Press Play                                                  │
│    3. Open Table of Contents                                     │
│    4. Click each chapter, play a few seconds                     │
│    5. Save the cover image                                       │
│                                                                  │
│  When done, stop recording and copy the generated code.          │
│  Save it to:                                                     │
└─────────────────────────────────────────────────────────────────┘
""")
        print(f"  {args.output}\n")

        # Opens the Playwright Inspector with Record capability
        await page.pause()

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
