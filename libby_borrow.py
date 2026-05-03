#!/usr/bin/env python3
"""
libby_borrow.py — Search Libby and borrow an audiobook.

Usage:
    python libby_borrow.py "Harry Potter and the Prisoner of Azkaban"

The script:
  1. Opens Libby in a browser using your existing session.
  2. Searches for the title.
  3. If an audiobook copy is available, borrows it immediately.
  4. If not available, reports the hold wait time across all libraries
     and tells you which has the shortest wait.

Requirements:
    pip install playwright browser-cookie3
    playwright install chromium
"""

import argparse
import asyncio
import re
from difflib import SequenceMatcher
from pathlib import Path

from playwright.async_api import async_playwright

LIBBY_SHELF = "https://libbyapp.com/shelf"
CHROMIUM_PROFILE = Path.home() / ".libby_to_yoto" / "chromium-profile"
FUZZY_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# Fuzzy matching (mirrors libby_to_yoto.py)
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace(" ", " ")).strip().lower()


def _partial_ratio(short: str, long: str) -> float:
    """fuzzywuzzy-style partial ratio: best ratio of `short` against any same-length window of `long`."""
    if not short or not long:
        return 0.0
    if len(short) > len(long):
        short, long = long, short
    matcher = SequenceMatcher(None, short, long)
    best = 0.0
    for block in matcher.get_matching_blocks():
        start = max(0, block.b - block.a)
        window = long[start : start + len(short)]
        r = SequenceMatcher(None, short, window).ratio()
        if r > best:
            best = r
    return best


def best_match(query: str, results: list[dict]) -> dict | None:
    """Return the highest-scoring result above FUZZY_THRESHOLD, or None."""
    q = _norm(query)
    scored = []
    for r in results:
        score = _partial_ratio(q, _norm(r["title"]))
        scored.append((score, r))
        print(f"  [{score:.2f}] {r['title']!r}  ({r['format']}, {r['status']})")
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] >= FUZZY_THRESHOLD:
        return scored[0][1]
    return None


async def launch_browser(pw):
    CHROMIUM_PROFILE.mkdir(parents=True, exist_ok=True)
    ctx = await pw.chromium.launch_persistent_context(
        str(CHROMIUM_PROFILE),
        headless=False,
        args=[
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
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
    return ctx


async def navigate_to_search(page):
    """Start from shelf and use JS to click the Search tab (direct URL nav is intercepted)."""
    await page.goto(LIBBY_SHELF, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    await page.evaluate("""() => {
        const tabs = document.querySelectorAll('[role="tab"]');
        tabs.forEach(t => { if (t.textContent.includes('Search')) t.click(); });
    }""")
    await page.wait_for_url("**/search/**", timeout=10_000)
    await page.wait_for_timeout(1000)


async def search(page, query: str, fmt: str = "audiobook"):
    """Fill the search box, submit, then activate the format filter."""
    await page.evaluate(f"""() => {{
        const box = document.querySelector('input[type="search"], [role="searchbox"]');
        if (!box) throw new Error('search box not found');
        box.focus();
        box.value = {repr(query)};
        box.dispatchEvent(new Event('input', {{bubbles: true}}));
    }}""")
    await page.keyboard.press("Enter")
    await page.wait_for_url("**/search/**query-**", timeout=10_000)
    await page.wait_for_timeout(1200)

    # Apply format filter by clicking the appropriate filter button.
    # This changes the URL from .../search/query-... to .../search/audiobooks/query-...
    # (or books/query-...) which Libby uses as its filtered search path.
    filter_label = "audiobook" if fmt == "audiobook" else "book"
    clicked = await page.evaluate(f"""() => {{
        const btn = Array.from(document.querySelectorAll('button')).find(b => {{
            const label = b.getAttribute('aria-label') || '';
            return label.toLowerCase().includes('{filter_label}') && label.includes('Filter');
        }});
        if (btn) {{ btn.click(); return true; }}
        return false;
    }}""")
    if clicked:
        await page.wait_for_url(f"**/search/{filter_label}s/**", timeout=6_000)
        await page.wait_for_timeout(800)


def _library_key_from_href(href: str | None) -> str | None:
    """Extract the library key from a borrow/hold URL's ?key= param."""
    if not href:
        return None
    m = re.search(r'[?&]key=([^&]+)', href)
    return m.group(1) if m else None


def _wait_weeks(status_label: str) -> str | None:
    """Extract '~N weeks' from a wait-list status label, or None."""
    m = re.search(r'(~\d+ weeks?)', status_label, re.IGNORECASE)
    return m.group(1) if m else None


async def _debug_headings(page):
    """Print all heading tags and their text to diagnose scraping issues."""
    headings = await page.evaluate("""() => {
        const out = [];
        ['h1','h2','h3','h4'].forEach(tag => {
            document.querySelectorAll(tag).forEach(el => {
                out.push(tag + ': ' + el.textContent.trim().slice(0, 100));
            });
        });
        return out;
    }""")
    print("  [debug headings]")
    for h in headings:
        print(f"    {h}")


async def scrape_results(page) -> list[dict]:
    """
    Return all search results as dicts with:
      title, format ('audiobook'|'book'|other), status, status_label,
      library_key, borrow_href, title_href
    """
    raw = await page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('h3').forEach(h => {
            // The title/format link is .title-tile-action; the first <a> is the author link.
            const titleLink = h.querySelector('a.title-tile-action');
            if (!titleLink) return;

            // Format from aria-label: "Audiobook: Title, by Author" or "Book: Title, by Author"
            const ariaLabel = titleLink.getAttribute('aria-label') || '';
            const lower = ariaLabel.toLowerCase();
            let fmt = 'other';
            if (lower.startsWith('audiobook:')) fmt = 'audiobook';
            else if (lower.startsWith('book:'))  fmt = 'book';

            // Title from the dedicated span
            const rawTitle = h.querySelector('.title-tile-title')?.textContent?.trim()
                || titleLink.textContent.trim();

            // Status from the .title-status div's aria-label (not the outer button)
            const card = h.closest('li, article') || h.parentElement?.parentElement;
            const statusDiv = card?.querySelector('.title-status');
            const statusLabel = statusDiv?.getAttribute('aria-label') || '';
            let status = 'unknown';
            if (statusLabel.toLowerCase().includes('available to borrow')) status = 'available';
            else if (statusLabel.toLowerCase().includes('wait list'))      status = 'waitlist';
            else if (statusLabel.toLowerCase().includes('borrowed'))       status = 'borrowed';

            // actionHref is the title detail page; the /request link lives there.
            out.push({
                title:       rawTitle,
                format:      fmt,
                status,
                statusLabel,
                actionHref:  titleLink.href || null,
                titleHref:   titleLink.href || null,
            });
        });
        return out;
    }""")
    # Attach parsed library key
    for r in raw:
        r['library_key'] = _library_key_from_href(r['actionHref'])
    return raw


async def scrape_per_library_availability(page, action_href: str) -> list[dict]:
    """
    Navigate to the borrow/hold request page, click the library name button to
    open the per-library comparison view, and return one row per library card.
    Each row: {library, status, detail}
    """
    # action_href is the title detail page; navigate there first, then click the /request link.
    await page.goto(action_href, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)

    request_href = await page.evaluate("""() => {
        const a = document.querySelector('a[href*="/request"]');
        return a ? a.href : null;
    }""")
    if not request_href:
        print(f"  [debug] No /request link found on detail page {action_href}")
        return []
    await page.evaluate(f"""() => {{
        const a = document.querySelector('a[href*="/request"]');
        if (a) a.click();
    }}""")
    await page.wait_for_url("**/request**", timeout=8_000)
    await page.wait_for_timeout(1200)

    # The request page shows the selected library as a button. Click it to open the comparison.
    SKIP = {"Place Hold", "Borrow", "Open Audiobook", "Keep Browsing",
            "Go To Shelf", "Back", "Read Sample", "Save"}
    clicked = await page.evaluate(f"""() => {{
        const skip = new Set({list(SKIP)});
        const btn = Array.from(document.querySelectorAll('button')).find(b => {{
            const t = b.textContent.trim();
            return t.length > 4 && !skip.has(t) && !b.disabled
                && !b.className.includes('crumb') && !b.className.includes('nav-')
                && !b.className.includes('app-') && !b.className.includes('notice');
        }});
        if (btn) {{ btn.click(); return btn.textContent.trim(); }}
        return null;
    }}""")

    if not clicked:
        # Dump visible button texts to help debug
        btns = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('button')).map(b => b.textContent.trim()).filter(t => t)
        """)
        print(f"  [debug] No library button found. Visible buttons: {btns}")
        return []

    print(f"  [debug] Clicked library button: {clicked!r}")
    await page.wait_for_selector('.circ-option-library-choice', timeout=6_000)
    await page.wait_for_timeout(500)

    return await page.evaluate("""() => {
        return Array.from(document.querySelectorAll('.circ-option-library-choice')).map(btn => {
            const statusEl  = btn.querySelector('.title-availability-status');
            const libraryEl = btn.querySelector('.title-availability-library');
            const detailEl  = btn.querySelector('.wait-list-summary');
            const status  = statusEl?.textContent?.trim()  || '';
            const library = libraryEl?.textContent?.trim().replace(/^at /, '') || '';
            const detail  = detailEl?.textContent?.trim()  || '';
            return { library, status, detail };
        });
    }""")


def print_search_report(query: str, fmt_results: dict[str, list[dict]]):
    """Print a human-readable per-library availability report."""
    print(f"\nAvailability report for: {query!r}\n")
    for fmt, rows in fmt_results.items():
        if not rows:
            continue
        print(f"  {fmt.capitalize()}:")
        for r in rows:
            status = r['status']
            lib    = r['library']
            detail = f"  ({r['detail']})" if r['detail'] else ''
            if 'available' in status.lower():
                print(f"    ✓ {status}  —  {lib}{detail}")
            elif 'no copies' in status.lower():
                print(f"    ✗ No copies  —  {lib}")
            else:
                print(f"    ~ {status}  —  {lib}{detail}")
        print()


async def confirm_borrow(page):
    """On the borrow confirmation page, click the Borrow button and return due date."""
    # Wait for the confirmation panel to appear
    await page.wait_for_selector("button:text('Borrow')", timeout=8_000)
    await page.evaluate("""() => {
        const btns = Array.from(document.querySelectorAll('button'));
        const b = btns.find(b => b.textContent.trim() === 'Borrow' && !b.disabled);
        if (b) b.click();
    }""")
    # Wait for success — look for "Borrowed until" text
    await page.wait_for_selector("p:has-text('Borrowed until')", timeout=10_000)
    due = await page.locator("p:has-text('Borrowed until')").text_content()
    return due.strip()


async def run(query: str, search_only: bool = False, fmt: str = "audiobook"):
    async with async_playwright() as pw:
        ctx = await launch_browser(pw)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print(f"Searching Libby for: {query!r}  [{fmt}]")
        await navigate_to_search(page)
        await search(page, query, fmt=fmt)

        await _debug_headings(page)
        results = await scrape_results(page)
        fmt_results = [r for r in results if r["format"] == fmt]

        if not results:
            print("\nNo results scraped at all — the page may not have loaded correctly.")
        elif not fmt_results:
            print(f"\nNo {fmt} results found. All scraped titles ({len(results)} total):")
            for r in results:
                print(f"  [{r['format']}] {r['title']!r}  (status: {r['status']})")

        print(f"\nFound {len(fmt_results)} {fmt} result(s) — scoring against query:")
        match = best_match(query, fmt_results)

        if match is None:
            print(f"\nNo {fmt} result matched {query!r} above the fuzzy threshold ({FUZZY_THRESHOLD}).")
            print("Try a more specific title, or check Libby manually.")
            await ctx.close()
            return

        print(f"\nBest match: {match['title']!r}  (status: {match['status']})")

        if search_only:
            if not match['actionHref']:
                print(f"No action link found for {match['title']!r} — it may already be borrowed or unavailable.")
                print(f"Status detail: {match['statusLabel']}")
            else:
                rows = await scrape_per_library_availability(page, match['actionHref'])
                if rows:
                    print_search_report(query, {fmt: rows})
                else:
                    print(f"Could not load per-library availability for {match['title']!r}.")
                    print(f"Overall status: {match['status']}  ({match['statusLabel']})")
        elif match["status"] == "available" and match["actionHref"]:
            print("Available now — borrowing…")
            # actionHref is the title detail page; navigate there and click the /request link.
            await page.goto(match["actionHref"], wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            await page.evaluate("""() => {
                const a = document.querySelector('a[href*="/request"]');
                if (a) a.click();
            }""")
            await page.wait_for_url("**/request**", timeout=8_000)
            await page.wait_for_timeout(1000)
            due = await confirm_borrow(page)
            print(f"Successfully borrowed! {due}")
        elif match["status"] == "waitlist":
            wait = _wait_weeks(match['statusLabel']) or 'unknown'
            key  = match['library_key'] or 'unknown library'
            print(f"Not available right now — wait list ({wait}) at library card: {key}")
        else:
            print(f"Status is {match['status']!r} — nothing to borrow automatically.")
            print(f"Status detail: {match['statusLabel']}")

        await page.wait_for_timeout(2000)
        await ctx.close()


def main():
    parser = argparse.ArgumentParser(description="Search Libby and borrow a title.")
    parser.add_argument("title", help="Book title to search for")
    parser.add_argument(
        "--search-only", "-s",
        action="store_true",
        help="Report availability across library cards without borrowing.",
    )
    parser.add_argument(
        "--ebook", "-e",
        action="store_true",
        help="Search for an ebook instead of an audiobook.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.title, search_only=args.search_only, fmt="book" if args.ebook else "audiobook"))


if __name__ == "__main__":
    main()
