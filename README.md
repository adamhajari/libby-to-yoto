# libby-to-yoto

Automates transferring a Libby audiobook to a Yoto playlist. Opens a Chromium browser, intercepts the CDN audio requests as it navigates every chapter, stitches and splits the audio into ~10-minute chunks, then creates a Yoto playlist and uploads everything.

## Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (or `pip`)
- ffmpeg (`brew install ffmpeg`)
- Chrome installed and signed into both Libby and Yoto

## Setup

```bash
uv sync
uv run playwright install chromium
```

Or with pip:

```bash
pip install -e .
playwright install chromium
```

## Usage

```bash
libby-to-yoto --title "The Book of Three"
```

This runs all three phases in sequence. You can also run phases individually:

```bash
libby-to-yoto --title "The Book of Three" --phase download
libby-to-yoto --title "The Book of Three" --phase process
libby-to-yoto --title "The Book of Three" --phase upload
```

## Phases

### 1. Download

Opens your Libby shelf in a browser window (using your existing Chrome session), finds the book, and navigates every chapter of the audiobook player. As it moves through the table of contents, it intercepts the signed CDN audio requests (`audioclips.cdn.overdrive.com`) and downloads each unique part.

Files are saved to `~/.libby_to_yoto/<book-slug>/Part01.mp3`, `Part02.mp3`, etc., with a `manifest.json` for deduplication. If interrupted, re-running the download phase resumes from where it left off.

### 2. Process

- **Stitches** all parts into a single `<title>_full.mp3`
- **Splits** into ~10-minute chunks named `<title>_chunk_000.mp3`, `<title>_chunk_001.mp3`, etc.
- **Converts** the cover art to JPEG and pads it to Yoto's 3:4 portrait ratio
- **Stages** chunks and cover into `/tmp/libby_to_yoto/uploads/<book-slug>/` for upload

### 3. Upload

Opens the Yoto playlist editor, creates a new playlist named after the book, uploads all audio chunks in one batch, waits for transcoding, uploads the cover art, then saves the playlist. Pauses at the end so you can verify track order and cover before closing.

## File layout

```
~/.libby_to_yoto/
  chromium-profile/          # persistent browser profile (keeps you logged in)
  <book-slug>/
    Part01.mp3 …             # raw Libby parts
    manifest.json            # hash → filename map (enables resume)
    <title>_full.mp3         # stitched audio
    <title>_chunk_000.mp3 …  # 10-min upload chunks
    <title>.jpg              # cover art (3:4 padded)

/tmp/libby_to_yoto/uploads/<book-slug>/   # staged for browser file picker
```

## Notes

- Neither Libby nor Yoto expose public APIs — everything is driven through a real browser via Playwright.
- The persistent Chromium profile at `~/.libby_to_yoto/chromium-profile` carries your login sessions across runs. Log in to Libby and Yoto there once.
- Signed CDN URLs expire; if a part fails to download, re-run the download phase to refresh the URLs and fill in the gap.
- The browser window stays visible so you can intervene if the automation gets stuck (e.g. a CAPTCHA or an unexpected dialog).
