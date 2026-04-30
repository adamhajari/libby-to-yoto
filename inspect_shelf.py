#!/usr/bin/env python3
"""Inspect the Libby shelf DOM to figure out the right book-finder selector."""
import asyncio
import browser_cookie3
from playwright.async_api import async_playwright


def load_chrome_cookies(domain):
    jar = browser_cookie3.chrome(domain_name=domain)
    cookies = []
    for c in jar:
        cookie = {"name": c.name, "value": c.value, "domain": c.domain,
                  "path": c.path, "httpOnly": bool(c.has_nonstandard_attr("HttpOnly")),
                  "secure": bool(c.secure)}
        if c.expires:
            cookie["expires"] = c.expires
        cookies.append(cookie)
    return cookies


async def main():
    async with async_playwright() as pw:
        from pathlib import Path
        user_data_dir = Path.home() / ".libby_to_yoto" / "chromium-profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        ctx = await pw.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=False,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            no_viewport=True,
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        await ctx.add_cookies(load_chrome_cookies("libbyapp.com"))

        page = await ctx.new_page()
        await page.goto("https://libbyapp.com/shelf", wait_until="networkidle")
        print(f"URL: {page.url}")

        # Wait until Libby renders more than the initial 2 skeleton buttons,
        # or until timeout (means not logged in / slow load)
        print("Waiting for shelf to render…")
        try:
            await page.wait_for_function("document.querySelectorAll('button').length > 5", timeout=15000)
        except Exception:
            print("⚠ Shelf did not render — likely not logged in. Please log in and press ENTER.")
            await asyncio.get_event_loop().run_in_executor(None, input, "Press ENTER after logging in… ")

        await asyncio.sleep(2)

        # Dump all button names so we can see what's actually on the page
        all_btns = await page.get_by_role("button").all()
        print(f"\nAll buttons on page ({len(all_btns)} total):")
        for btn in all_btns:
            try:
                name = (await btn.get_attribute("aria-label") or await btn.inner_text()).strip()
                if name:
                    print(f"  {name!r}")
            except Exception:
                pass

        # Find all "Open Audiobook" buttons and print 3 ancestor levels of text + tag
        import re
        btns = page.get_by_role("button", name=re.compile("Open Audiobook", re.IGNORECASE))
        count = await btns.count()
        print(f"\nFound {count} 'Open Audiobook' buttons on shelf\n")

        for i in range(count):
            btn = btns.nth(i)
            print(f"--- Button {i} ---")
            # Walk up 1, 2, 3 ancestors and print tag + truncated text
            for level in range(1, 5):
                xpath = "/".join([".."] * level)
                try:
                    ancestor = btn.locator(f"xpath={xpath}")
                    tag = await ancestor.evaluate("el => el.tagName.toLowerCase()")
                    classes = await ancestor.evaluate("el => el.className")
                    text = (await ancestor.inner_text()).replace("\n", " ").strip()[:120]
                    print(f"  +{level}: <{tag} class='{classes}'> {text!r}")
                except Exception as e:
                    print(f"  +{level}: error — {e}")
            print()

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
