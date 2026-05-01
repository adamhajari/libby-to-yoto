#!/usr/bin/env python3
"""
libby_to_yoto.py — Download a Libby audiobook and upload it to Yoto as a playlist.

Usage:
    python libby_to_yoto.py --title "Book Title" [--phase all|download|process|upload] [--force-split]

Phases:
    download  — Open Libby in a browser; intercept CDN audio requests as you navigate the TOC.
    process   — Stitch parts → split into ~10-min chunks → convert cover art.
    upload    — Create a Yoto playlist and upload chunks + cover.
    all       — Run all three phases in sequence (default).

Requirements:
    pip install playwright requests
    playwright install chromium
    brew install ffmpeg
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

import browser_cookie3
import requests
from PIL import Image as PILImage
from playwright.async_api import async_playwright, BrowserContext, expect

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKSPACE = Path.home() / ".libby_to_yoto" 
TMP_UPLOADS = Path("/tmp/libby_to_yoto/uploads")

LIBBY_SHELF = "https://libbyapp.com/shelf"
YOTO_PLAYLISTS = "https://my.yotoplay.com/my-cards/playlists"
CDN_HOST = "audioclips.cdn.overdrive.com"

CHUNK_MINUTES = 10  # target chunk length


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def book_dir(title: str) -> Path:
    return WORKSPACE / slug(title)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def ffmpeg(*args):
    run(["ffmpeg", "-y", *[str(a) for a in args]])


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def normalize_cdn_key(url: str) -> str:
    """Normalize a CDN URL for dedupe while keeping meaningful segment params.

    Libby rotates signature/auth parameters frequently; those should not create
    new parts. Overdrive CDN embeds auth tokens directly in the URL path
    (e.g. /expiretime=...;ctime=...;badurl=.../SIGNATURE/full/bucket/filehash).
    Strip those by keeping only the stable suffix starting at '/full/'.
    """
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path
    # Overdrive CDN: auth is path-prefixed; stable content starts at /full/
    stable = re.search(r"/full/.+", path)
    if stable:
        path = stable.group(0)
    q = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    keep_keys = {
        "start",
        "end",
        "offset",
        "range",
        "from",
        "to",
        "clip",
        "part",
        "segment",
        "dur",
        "duration",
        "t",
    }
    kept = [(k, v) for (k, v) in q if k.lower() in keep_keys]
    kept.sort()
    query = urllib.parse.urlencode(kept, doseq=True)
    base = f"{parsed.scheme}://{parsed.netloc}{path}"
    return f"{base}?{query}" if query else base


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def load_chrome_cookies(domain: str) -> list[dict]:
    """Read cookies for a domain from Chrome's local store (macOS Keychain-aware)."""
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


async def inject_cookies(ctx: BrowserContext, domain: str):
    cookies = load_chrome_cookies(domain)
    if cookies:
        await ctx.add_cookies(cookies)
        print(f"  ✓ Injected {len(cookies)} Chrome cookies for {domain}")
    else:
        print(f"  ⚠ No Chrome cookies found for {domain} — you may need to log in manually")


# ---------------------------------------------------------------------------
# Phase 1 — Download
# ---------------------------------------------------------------------------



async def read_chapters_from_toc(frame) -> list[dict]:
    """Open the player's Table of Contents dialog and parse chapter names + start times.

    Each TOC row is a <li> with two buttons: the chapter name and the start time.
    The time button's textContent ends in MM:SS or H:MM:SS, which is far more
    reliable than scraping the player's running elapsed-time display.
    """
    toc_btn = frame.get_by_role("button", name=re.compile(r"Table of Contents", re.IGNORECASE))
    await toc_btn.first.click()

    heading = frame.get_by_role("heading", name=re.compile(r"^Contents$", re.IGNORECASE))
    await heading.first.wait_for(timeout=5000)
    await asyncio.sleep(0.3)  # let the list finish rendering

    rows = await frame.locator("li").evaluate_all(
        """
        (items) => items
            .filter(li => li.querySelectorAll('button').length === 2)
            .map(li => {
                const btns = li.querySelectorAll('button');
                return {
                    name: (btns[0].textContent || '').trim(),
                    time_text: (btns[1].textContent || '').trim(),
                };
            })
        """
    )

    # Each time_text is e.g. "4 minutes 28 seconds04:28" — screen-reader text
    # then the visible MM:SS or H:MM:SS clock with no separator. Anchor to the end
    # so we never grab digits from the SR prefix.
    time_re = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})$")
    chapters: list[dict] = []
    for r in rows:
        m = time_re.search(r["time_text"])
        if not m:
            print(f"  ⚠ Could not parse chapter time from {r['time_text']!r}")
            continue
        h = int(m.group(1)) if m.group(1) else 0
        mm = int(m.group(2))
        ss = int(m.group(3))
        chapters.append({
            "name": r["name"],
            "start_seconds": float(h * 3600 + mm * 60 + ss),
        })

    try:
        await frame.get_by_role("button", name=re.compile(r"Dismiss dialog", re.IGNORECASE)).first.click(timeout=3000)
        await asyncio.sleep(0.3)
    except Exception:
        pass

    return chapters


async def navigate_toc(page, capturing: list, on_capture_start=None) -> tuple[int, list[dict]]:
    """
    Rewind to chapter 1, scrape chapter timestamps from the TOC dialog, then play
    and step through every chapter with 'Next Chapter' so the CDN fires for each
    audio segment. on_capture_start: optional callback invoked just after capturing
    is enabled. Returns (number_of_chapters_clicked, chapters).
    """
    frame = page.frame_locator("iframe")

    # Dismiss the "Synchronize position?" alert if Libby shows it on open.
    stay_btn = frame.get_by_role("button", name=re.compile(r"Stay at", re.IGNORECASE))
    try:
        await stay_btn.first.wait_for(timeout=3000)
        await stay_btn.first.click()
        await asyncio.sleep(0.5)
        print("  Dismissed position sync dialog")
    except Exception:
        pass

    # Rewind to chapter 1 by clicking "Previous Chapter" until it's gone (replaced by
    # "Start Of Audiobook [disabled]"). Use a longer timeout on the first check because
    # the player may still be loading audio when opened at the last chapter position.
    # At the very end of the book the left button is "Start Of Chapter" (not "Previous
    # Chapter"), so we match both. Clicking "Start Of Chapter" jumps to the start of the
    # last chapter, after which "Previous Chapter" reappears for the remaining rewind.
    # The loop exits when neither button is present (replaced by "Start Of Audiobook [disabled]").
    print("  Rewinding to beginning…")
    prev_btn = frame.get_by_role("button", name=re.compile(r"(Previous Chapter|Start Of Chapter)", re.IGNORECASE))
    first_check = True
    while True:
        timeout = 10000 if first_check else 3000
        first_check = False
        try:
            await prev_btn.first.wait_for(timeout=timeout)
            if await prev_btn.first.is_disabled():
                break
            await prev_btn.first.click()
            await asyncio.sleep(1)
        except Exception:
            break  # "Start Of Audiobook [disabled]" — at chapter 1

    # Read chapter timestamps from the TOC dialog (authoritative, no playback needed).
    chapters = await read_chapters_from_toc(frame)
    print(f"  Read {len(chapters)} chapters from TOC")
    for i, ch in enumerate(chapters):
        print(f"    [{i + 1}] {ch['name'][:60]} @ {ch['start_seconds']:.0f}s")

    # Enable CDN capture now that we're at the start.
    capturing[0] = True
    if on_capture_start:
        on_capture_start()

    # Play so the first segment's CDN request fires.
    play_btn = frame.get_by_role("button", name="Play", exact=True)
    try:
        await play_btn.first.wait_for(timeout=5000)
        await play_btn.first.click()
        print("  ▶ Playing from beginning")
        await asyncio.sleep(3)
    except Exception:
        pass

    # Match the right-side navigation button "Next Chapter . X minutes ahead."
    # but NOT the center chapter-label button "Next Chapter: Owl Post . Table of Contents"
    # (which appears in bonus preview sections and opens the TOC when clicked).
    # The nav button uses a space-dot pattern; the chapter label uses a colon.
    next_btn = frame.get_by_role("button", name=re.compile(r"^Next Chapter\s+\.", re.IGNORECASE))
    clicked = 0
    while True:
        try:
            await next_btn.first.wait_for(timeout=8000, state="visible")
        except Exception:
            print("  ✓ No Next Chapter button — end of book")
            break
        if await next_btn.first.is_disabled():
            print("  ✓ Next Chapter disabled — end of book")
            break
        await next_btn.first.click()
        clicked += 1
        await asyncio.sleep(2.0)  # wait for CDN pre-buffer requests

    return clicked, chapters


async def save_cover(page, bdir: Path, title: str) -> Path | None:
    """Download the cover image shown in the Libby player."""
    title_slug = slug(title)
    cover_dest = bdir / f"{title_slug}.jpg"
    if cover_dest.exists():
        return cover_dest
    try:
        # Cover images on the shelf page each have alt="Audiobook: '<Title>'. Cover image."
        # Match by title substring so we pick the right book when multiple covers are visible.
        src = await page.evaluate("""(title) => {
            const lower = title.toLowerCase();
            const img = Array.from(document.querySelectorAll('img[alt*="Cover image"]'))
                .find(i => i.alt.toLowerCase().includes(lower));
            return img ? img.src : null;
        }""", title)
        if not src:
            return None
        # Shelf serves IMG100; swap to IMG400 for higher resolution.
        src = re.sub(r'IMG100\.JPG', 'IMG400.JPG', src, flags=re.IGNORECASE)
        r = requests.get(src, timeout=30)
        r.raise_for_status()
        # Save raw bytes; convert .webp → .jpg via ffmpeg if needed
        raw = bdir / f"{title_slug}_cover_raw"
        raw.write_bytes(r.content)
        ffmpeg("-i", raw, cover_dest)
        raw.unlink(missing_ok=True)
        print(f"  ✓ Cover saved → {cover_dest.name}")
        return cover_dest
    except Exception as e:
        print(f"  ⚠ Could not auto-save cover: {e}. Save it manually to {cover_dest}")
        return None


async def phase_download(title: str, ctx: BrowserContext, debug_cdn: bool = False):
    bdir = book_dir(title)
    bdir.mkdir(parents=True, exist_ok=True)
    manifest_path = bdir / "manifest.json"

    manifest: dict[str, str] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        print(f"Resuming — {len(manifest)} parts already captured.")

    # Dedupe by normalized CDN key (path + meaningful segment params).
    seen_keys: set[str] = {normalize_cdn_key(k) for k in manifest.keys()}
    part_counter = [len(manifest) + 1]
    capturing = [False]
    lock = asyncio.Lock()

    # CDN URLs fired during the rewind (before capturing is enabled), in rewind order
    # (= reverse book order). Flushed in reverse when capturing starts → correct Part order.
    pre_capture_order: list[str] = []   # normalized keys, first-seen order
    pre_capture_urls: dict[str, str] = {}  # normalized key → latest signed URL
    seen_debug_requests: set[str] = set()

    async def save_part(url: str, dedupe_key: str):
        async with lock:
            if dedupe_key in seen_keys:
                print(f"  [CDN duplicate, skipped]  {Path(urllib.parse.urlsplit(url).path).name}")
                return
            seen_keys.add(dedupe_key)
            part_num = part_counter[0]
            part_counter[0] += 1

        fname = f"Part{part_num:02d}.mp3"
        dest = bdir / fname
        print(f"\n  Captured: {Path(urllib.parse.urlsplit(url).path).name}")
        print(f"  → {fname}")

        try:
            r = requests.get(url, timeout=120, stream=True)
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
            print(f"  ✓ Saved {dest.stat().st_size // 1024} KB")
        except Exception as e:
            print(f"  ✗ Download failed: {e}")
            async with lock:
                seen_keys.discard(dedupe_key)
                part_counter[0] -= 1
            return

        async with lock:
            manifest[url] = fname
            manifest_path.write_text(json.dumps(manifest, indent=2))

    async def on_response(response):
        url = response.url
        if CDN_HOST not in url:
            return
        dedupe_key = normalize_cdn_key(url)
        if debug_cdn:
            req = response.request
            req_range = req.headers.get("range") or req.headers.get("Range") or "-"
            status = response.status
            h = response.headers
            content_range = h.get("content-range") or h.get("Content-Range") or "-"
            content_length = h.get("content-length") or h.get("Content-Length") or "-"
            if dedupe_key not in seen_debug_requests:
                seen_debug_requests.add(dedupe_key)
                print(f"  [CDN seen] {dedupe_key}")
            print(
                "  [CDN net] "
                f"status={status} req_range={req_range} "
                f"content_range={content_range} content_length={content_length}"
            )
        if not capturing[0]:
            if dedupe_key not in pre_capture_urls:
                pre_capture_order.append(dedupe_key)
                print(f"  [CDN pre-capture, buffered] {Path(urllib.parse.urlsplit(url).path).name}")
            pre_capture_urls[dedupe_key] = url  # keep freshest signed URL
            return
        asyncio.ensure_future(save_part(url, dedupe_key))

    def flush_pre_capture():
        # Pre-capture fired in reverse book order (last part → first); reverse to get Part01 first.
        for dedupe_key in reversed(pre_capture_order):
            asyncio.ensure_future(save_part(pre_capture_urls[dedupe_key], dedupe_key))

    page = await ctx.new_page()
    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    print(f"\nOpening Libby shelf…")
    await page.goto(LIBBY_SHELF, wait_until="domcontentloaded")
    await asyncio.sleep(2)

    # The shelf lazy-loads cards as you scroll; wheel-scroll to force all loans into the DOM.
    print(f"Waiting for shelf to render…")
    try:
        await page.get_by_role("button", name="Open Audiobook").first.wait_for(timeout=20000)
    except Exception:
        pass
    # Send real wheel events so Libby's SPA scroll handler fires.
    for i in range(30):
        found_heading = await page.get_by_role("heading", name=re.compile(re.escape(title), re.IGNORECASE)).count()
        if found_heading:
            print(f"  ✓ Heading found after {i} scroll steps")
            break
        await page.mouse.wheel(0, 800)
        await asyncio.sleep(0.3)
    else:
        # Count how many h3 headings and Open Audiobook buttons are visible for debugging
        h3_count = await page.locator("h3").count()
        oa_count = await page.get_by_role("button", name="Open Audiobook").count()
        print(f"  ⚠ Heading not found after scrolling (h3s={h3_count}, 'Open Audiobook' buttons={oa_count})")

    # Find the book by title: locate the h3 whose text contains the title, then click
    # the first "Open Audiobook" button that follows it in document order.
    print(f"Looking for '{title}' on shelf…")
    found = await page.evaluate("""
        (searchTitle) => {
            const lower = searchTitle.toLowerCase();
            const normalize = s => s.replace(/ /g, ' ').toLowerCase();
            const heading = Array.from(document.querySelectorAll('h3')).find(
                h => normalize(h.textContent).includes(lower)
            );
            if (!heading) return false;
            const openBtns = Array.from(document.querySelectorAll('button')).filter(
                b => /open audiobook/i.test(b.textContent + ' ' + (b.getAttribute('aria-label') || ''))
            );
            // Pick the first Open Audiobook button that comes after the heading in DOM order
            const btn = openBtns.find(
                b => heading.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING
            );
            if (btn) { btn.click(); return true; }
            return false;
        }
    """, title)

    # Grab cover while shelf is loaded — all book covers are in the DOM here.
    await save_cover(page, bdir, title)

    if found:
        print(f"  ✓ Found and opened '{title}'")
    else:
        # Debug: print all button names so we can see what's on the page
        all_btns = await page.get_by_role("button").all()
        btn_names = []
        for b in all_btns:
            try:
                n = (await b.get_attribute("aria-label") or await b.inner_text()).strip()
                if n:
                    btn_names.append(n)
            except Exception:
                pass
        print(f"  ⚠ Could not find '{title}' on shelf.")
        print(f"  Buttons visible: {btn_names[:20]}")
        print(f"  Please open the audiobook player in the browser, then press ENTER.")
        await asyncio.get_event_loop().run_in_executor(None, input, "Press ENTER once the player is open… ")

    # Wait for the iframe player to load and its footer controls to render
    print("Waiting for player…")
    await page.locator("iframe").wait_for(timeout=20000)
    frame = page.frame_locator("iframe")
    await frame.get_by_role("button", name=re.compile(r"Table of Contents", re.IGNORECASE)).first.wait_for(timeout=60000)
    await asyncio.sleep(1)

    # Navigate every TOC entry
    print("\nNavigating TOC…")
    n, chapters = await navigate_toc(page, capturing, on_capture_start=flush_pre_capture)
    print(f"\n  TOC navigation complete — {n} entries clicked, {len(chapters)} chapters recorded")

    # Save chapter timestamps alongside the manifest.
    chapters_path = bdir / "chapters.json"
    chapters_path.write_text(json.dumps(chapters, indent=2))
    print(f"  ✓ Saved chapter timestamps → {chapters_path.name}")

    # Brief wait for any in-flight downloads to finish
    await asyncio.sleep(5)

    await page.close()

    print(f"\n✓ Captured {len(manifest)} unique audio parts.")
    if not manifest:
        sys.exit("No audio files captured — nothing to process.")

    return manifest


# ---------------------------------------------------------------------------
# Phase 2 — Process (stitch + split + cover)
# ---------------------------------------------------------------------------

COVER_RATIO = (3, 4)  # Yoto card artwork: portrait 3:4

def pad_cover_to_ratio(src: Path, dst: Path, ratio: tuple[int, int] = COVER_RATIO) -> None:
    """Add black bars to fit src image into the target aspect ratio, save to dst."""
    img = PILImage.open(src).convert("RGB")
    w, h = img.size
    target_w, target_h = ratio
    if w * target_h > h * target_w:
        # wider than target — add top/bottom bars
        new_h = w * target_h // target_w
        canvas = PILImage.new("RGB", (w, new_h), (0, 0, 0))
        canvas.paste(img, (0, (new_h - h) // 2))
    else:
        # taller than target — add left/right bars
        new_w = h * target_w // target_h
        canvas = PILImage.new("RGB", (new_w, h), (0, 0, 0))
        canvas.paste(img, ((new_w - w) // 2, 0))
    canvas.save(dst, quality=95)

def _chapter_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def _find_chunks(chunk_dir: Path, title_slug: str) -> list[Path]:
    """Return existing chunk files regardless of naming scheme, sorted by name."""
    # Chapter-based: 001_chapter-name_title-slug.mp3
    chapter_chunks = sorted(chunk_dir.glob(f"[0-9][0-9][0-9]_*_{title_slug}.mp3"))
    # Time-based (legacy): title-slug_chunk_000.mp3
    time_chunks = sorted(chunk_dir.glob(f"{title_slug}_chunk_*.mp3"))
    return chapter_chunks or time_chunks


def _split_by_chapters(full_mp3: Path, chunk_dir: Path, title_slug: str, chapters_path: Path) -> None:
    chapters = json.loads(chapters_path.read_text())
    duration = ffprobe_duration(full_mp3)
    print(f"[Process] Splitting by {len(chapters)} chapters (audio duration {duration / 60:.1f} min)…")

    # Build strictly-increasing cut points, discarding zeros and backward jumps.
    # A zero start_seconds means the position read failed; treat those chapters as
    # having no known boundary and skip them as cut points (they'll merge into the
    # previous segment).
    clean_chapters: list[dict] = [chapters[0]]  # chapter 1 always starts at 0
    prev_t = 0.0
    for ch in chapters[1:]:
        t = ch["start_seconds"]
        if t > prev_t:
            clean_chapters.append(ch)
            prev_t = t
        else:
            print(f"  [skip bad timestamp] {ch['name']!r} @ {t:.0f}s (prev={prev_t:.0f}s)")

    # FFmpeg ignores segment_times past EOF → one giant file. Drop chapters whose
    # start lies beyond this encode so cuts stay inside the mux.
    eof_eps = 0.05
    usable: list[dict] = [clean_chapters[0]]
    for ch in clean_chapters[1:]:
        if ch["start_seconds"] < duration - eof_eps:
            usable.append(ch)
        else:
            print(
                f"  [skip past end of file] {ch['name']!r} @ {ch['start_seconds']:.0f}s "
                f"(duration={duration:.0f}s)"
            )

    if len(usable) < 2:
        print(
            "[Process] Not enough in-file chapter timestamps — falling back to time-based split."
        )
        _split_by_time(full_mp3, chunk_dir, title_slug)
        return

    if len(usable) < len(clean_chapters):
        print(
            f"[Process] Using {len(usable)} chapter starts inside the file "
            f"({len(clean_chapters) - len(usable)} past-EOF dropped)"
        )
    else:
        print(
            f"[Process] Using {len(usable)} chapters after filtering "
            f"({len(chapters) - len(clean_chapters)} bad timestamps dropped)"
        )

    tmp_pattern = chunk_dir / f"{title_slug}_chunk_%03d.mp3"
    cut_points = ",".join(f"{c['start_seconds']:.3f}" for c in usable[1:])
    ffmpeg(
        "-i", full_mp3,
        "-f", "segment",
        "-segment_times", cut_points,
        "-c", "copy",
        "-reset_timestamps", "1",
        tmp_pattern,
    )

    raw_chunks = sorted(chunk_dir.glob(f"{title_slug}_chunk_*.mp3"))
    for i, chunk_path in enumerate(raw_chunks):
        name = usable[i]["name"] if i < len(usable) else f"chapter-{i + 1}"
        new_path = chunk_dir / f"{i + 1:03d}_{_chapter_slug(name)}_{title_slug}.mp3"
        chunk_path.rename(new_path)
    print(f"[Process] ✓ {len(raw_chunks)} chapter chunks ready.")


def _split_by_time(full_mp3: Path, chunk_dir: Path, title_slug: str) -> None:
    duration = ffprobe_duration(full_mp3)
    chunk_secs = CHUNK_MINUTES * 60
    n_chunks = int(duration / chunk_secs) + 1
    print(f"[Process] Splitting {duration / 60:.1f} min → ~{n_chunks} chunks of {CHUNK_MINUTES} min")
    ffmpeg(
        "-i", full_mp3,
        "-f", "segment",
        "-segment_time", str(chunk_secs),
        "-c", "copy",
        "-reset_timestamps", "1",
        chunk_dir / f"{title_slug}_chunk_%03d.mp3",
    )


def phase_process(title: str, pad_cover: bool = True, force_split: bool = False):
    bdir = book_dir(title)
    manifest_path = bdir / "manifest.json"

    if not manifest_path.exists():
        sys.exit(f"No manifest found at {manifest_path}. Run the download phase first.")

    manifest: dict[str, str] = json.loads(manifest_path.read_text())
    parts = sorted(manifest.values(), key=lambda n: int(re.search(r"\d+", n).group()))
    part_paths = [bdir / p for p in parts]
    missing = [p for p in part_paths if not p.exists()]
    if missing:
        sys.exit(f"Missing part files: {missing}")

    title_slug = slug(title)
    full_mp3 = bdir / f"{title_slug}_full.mp3"
    chunk_dir = bdir  # chunks live alongside parts
    cover_src = bdir / f"{title_slug}.jpg"  # may also be .webp

    # --- Stitch ---
    if not full_mp3.exists():
        print(f"\n[Process] Stitching {len(parts)} parts → {full_mp3.name}")
        concat_list = bdir / "concat.txt"
        concat_list.write_text("\n".join(f"file '{p.resolve()}'" for p in part_paths))
        ffmpeg("-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", full_mp3)
    else:
        print(f"[Process] Full file already exists — skipping stitch.")

    # --- Convert cover .webp → .jpg ---
    webp_cover = bdir / f"{title_slug}.webp"
    if webp_cover.exists() and not cover_src.exists():
        print(f"[Process] Converting cover {webp_cover.name} → {cover_src.name}")
        ffmpeg("-i", webp_cover, cover_src)

    # --- Pad cover to 3:4 portrait ratio with black fill ---
    if pad_cover and cover_src.exists():
        print(f"[Process] Padding cover to {COVER_RATIO[0]}:{COVER_RATIO[1]} ratio…")
        pad_cover_to_ratio(cover_src, cover_src)
        print(f"[Process] ✓ Cover padded.")

    # --- Split into chunks ---
    # Check for both naming schemes (chapter-based and legacy time-based).
    existing_chunks = _find_chunks(chunk_dir, title_slug)
    if force_split and existing_chunks:
        print(f"[Process] --force-split: removing {len(existing_chunks)} existing chunks.")
        for p in existing_chunks:
            p.unlink(missing_ok=True)
        existing_chunks = []
    if existing_chunks:
        print(f"[Process] {len(existing_chunks)} chunks already exist — skipping split.")
    else:
        chapters_path = bdir / "chapters.json"
        if chapters_path.exists():
            _split_by_chapters(full_mp3, chunk_dir, title_slug, chapters_path)
        else:
            _split_by_time(full_mp3, chunk_dir, title_slug)
        existing_chunks = _find_chunks(chunk_dir, title_slug)

    print(f"[Process] ✓ {len(existing_chunks)} chunks ready.")

    # --- Stage for browser file picker ---
    upload_dir = TMP_UPLOADS / slug(title)
    upload_dir.mkdir(parents=True, exist_ok=True)
    print(f"[Process] Staging files in {upload_dir}")
    for f in upload_dir.iterdir():
        f.unlink()
    for chunk in existing_chunks:
        shutil.copy2(chunk, upload_dir / chunk.name)
    if cover_src.exists():
        shutil.copy2(cover_src, upload_dir / cover_src.name)

    print(f"[Process] ✓ Staged {len(existing_chunks)} chunks + cover → {upload_dir}")
    return existing_chunks, cover_src if cover_src.exists() else None


# ---------------------------------------------------------------------------
# Phase 3 — Upload to Yoto
# ---------------------------------------------------------------------------

async def phase_upload(title: str, chunks: list[Path], cover: Path | None, ctx: BrowserContext):
    upload_dir = TMP_UPLOADS / slug(title)

    page = await ctx.new_page()
    print(f"\n[Upload] Opening Yoto playlists: {YOTO_PLAYLISTS}")
    await page.goto(YOTO_PLAYLISTS, wait_until="networkidle", timeout=30000)

    if "login" in page.url.lower() or "sign-in" in page.url.lower():
        print("  ⚠ Yoto cookie import didn't carry the session — please log in in the browser window.")
        await asyncio.get_event_loop().run_in_executor(None, input, "Press ENTER after logging in… ")
        await page.goto(YOTO_PLAYLISTS, wait_until="networkidle", timeout=30000)

    # Click "Add Playlist" link (navigates to /card/edit)
    print("[Upload] Looking for 'Add Playlist' link…")
    add_btn = page.get_by_role("link", name=re.compile(r"add playlist", re.IGNORECASE)).first
    await add_btn.wait_for(timeout=15000)
    await add_btn.click()
    await page.wait_for_load_state("networkidle")

    # Set playlist name — field uses placeholder "Playlist name", not a label
    print(f"[Upload] Setting playlist name to: {title}")
    name_field = page.get_by_placeholder("Playlist name")
    await name_field.wait_for(timeout=10000)
    await name_field.fill(title)

    # Upload audio chunks — "Add audio" is a clickable div that opens a file chooser
    chunk_paths = sorted(upload_dir.glob("*.mp3"))
    if not chunk_paths:
        sys.exit(f"No chunk files found in {upload_dir}")

    print(f"[Upload] Uploading {len(chunk_paths)} audio chunks…")
    async with page.expect_file_chooser() as fc_info:
        await page.get_by_text("Add audio").click()
    file_chooser = await fc_info.value
    await file_chooser.set_files([str(p) for p in chunk_paths])
    print("[Upload] Files submitted — waiting for transcoding to complete…")
    # Wait until no track still shows "Transcoding"
    await page.locator("text=Transcoding").first.wait_for(state="hidden", timeout=300000)
    print("[Upload] ✓ Audio transcoding complete.")

    # Upload cover art — "Upload Art" is a clickable div that opens a file chooser
    if cover and cover.exists():
        cover_staged = upload_dir / cover.name
        print(f"[Upload] Uploading cover art: {cover_staged.name}")
        async with page.expect_file_chooser() as fc_info:
            await page.get_by_text("Upload Art").click()
        cover_chooser = await fc_info.value
        await cover_chooser.set_files(str(cover_staged))
        await page.wait_for_load_state("networkidle", timeout=30000)
        print("[Upload] ✓ Cover uploaded.")
    else:
        print("[Upload] ⚠ No cover image found — skipping artwork upload.")

    # Save / Create — button stays disabled until cover art finishes processing
    print("[Upload] Saving playlist…")
    save_btn = page.get_by_role("button", name=re.compile(r"^(save|create)$", re.IGNORECASE)).first
    await save_btn.wait_for(state="visible", timeout=30000)
    await expect(save_btn).to_be_enabled(timeout=300000)
    await save_btn.click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    print(f"[Upload] ✓ Playlist '{title}' created at {page.url}")

    print("""
┌─────────────────────────────────────────────────────────────────┐
│  Please verify in the browser:                                   │
│   • Track order matches the audiobook                           │
│   • No duplicate tracks                                         │
│   • Cover art is correct                                        │
│  Press ENTER to close the browser.                              │
└─────────────────────────────────────────────────────────────────┘
""")
    await asyncio.get_event_loop().run_in_executor(None, input, "Press ENTER to finish… ")
    await page.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Libby → Yoto audiobook pipeline")
    parser.add_argument("--title", required=True, help="Audiobook title (used for filenames and playlist name)")
    parser.add_argument("--phase", choices=["all", "download", "process", "upload"], default="all")
    parser.add_argument("--no-pad-cover", action="store_true", help="Skip padding the cover image to 3:4 ratio")
    parser.add_argument(
        "--force-split",
        action="store_true",
        help="Delete existing chunk mp3s and re-run stitch split (chapter or time-based)",
    )
    parser.add_argument(
        "--debug-cdn",
        action="store_true",
        help="Log every observed Libby CDN URL key during download",
    )
    args = parser.parse_args()

    title = args.title
    phase = args.phase

    WORKSPACE.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        # Launch persistent Chromium so existing Libby/Yoto sessions are available
        user_data_dir = Path.home() / ".libby_to_yoto" / "chromium-profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)

        ctx = await pw.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=False,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            no_viewport=True,
            ignore_https_errors=True,
        )

        # Mask automation signals so sites like Libby don't block login
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        """)

        print("Importing Chrome cookies…")
        await inject_cookies(ctx, "libbyapp.com")
        await inject_cookies(ctx, "my.yotoplay.com")

        chunks, cover = None, None

        if phase in ("all", "download"):
            await phase_download(title, ctx, debug_cdn=args.debug_cdn)

        if phase in ("all", "process"):
            chunks, cover = phase_process(
                title,
                pad_cover=not args.no_pad_cover,
                force_split=args.force_split,
            )

        if phase in ("all", "upload"):
            if chunks is None:
                # Load from disk if we skipped process phase
                bdir = book_dir(title)
                title_slug = slug(title)
                chunks = _find_chunks(bdir, title_slug)
                cover_path = bdir / f"{title_slug}.jpg"
                cover = cover_path if cover_path.exists() else None
            await phase_upload(title, chunks, cover, ctx)

        await ctx.close()

    print("\n✓ All done!")


def main_sync():
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
