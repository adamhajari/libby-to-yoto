# Audiobook to Yoto Workflow

This document is the single reference for the process. It combines the original step-by-step instructions with the reusable notes and scripts.

## Goal

Convert a Libby audiobook into a Yoto playlist.

Notes:

* Neither Libby nor Yoto have APIs. All steps involving either Libby or Yoto must be performed using Chromium.  
* You should already be signed into both Libby and Yoto in Chromium.  
* If you are not signed into Yoto use the saved credentials to log in.  
* If you are not signed into Libby ask me for help

## Core URLs

- Libby: https://libbyapp.com  
- Libby shelf: https://libbyapp.com/shelf  
- Libby search: https://libbyapp.com/search/spl  
- Yoto playlists: https://my.yotoplay.com/my-cards/playlists

## 1\) Downloading the audiobook files

1. Open Libby in an interactive Chromium session.  
2. Check whether the book is already on your shelf.  
3. If not, search for it and borrow it.  
4. Open the audiobook player and follow these instructions exactly. Do not attempt to take shortcuts by searching for an API or using the player shell "COPY" control. BRUTE FORCE WALKING THROUGH EACH CHAPTER IS THE FASTEST METHOD:  
   - Play the audiobook and download the audio file that plays.  
   - Use the Table of Contents menu to navigate through all sections/chapters of the book. Play only a few seconds at each new section to force Libby to reveal every MP3 part.  
   - The reliable approach is to trigger the browser’s signed audioclips.cdn.overdrive.com requests from the player.  
   - If a segment URL expires, revisit that section and trigger it again.  
   - Reuse the signed-in browser session whenever possible.  
   - A single MP3 file often covers multiple sections so don't download duplicate files.  
   - Keep track of file order as you download.  
5. Save the cover image. If the cover file saves as a .webp file convert it to .jpg using ffmpeg.

## 2\) Split the audio files

- Split the audiobook into \~10 minute chunks.  
- Keep all files in order.  
- Prefix chunk filenames with the title so they’re self-describing.

### Recommended local file layout

- Per-book work folder: /home/user/.libby\_to\_yoto/workspace/libby\_to\_yoto  
- /\<book-slug\>/  
- Source parts: /home/user/.libby\_to\_yoto/workspace/libby\_to\_yoto  
- /\<book-slug\>/Part01.mp3, Part02.mp3, etc.  
- Full stitched file: /home/user/.libby\_to\_yoto/workspace/libby\_to\_yoto  
- /\<book-slug\>/\<title\>\_full.mp3  
- Chunk output: /home/user/.libby\_to\_yoto/workspace/libby\_to\_yoto  
- /\<book-slug\>/\<title\>\_chunk\_000.mp3, \<title\>\_chunk\_001.mp3, etc.  
- Cover image: /home/user/.libby\_to\_yoto/workspace/libby\_to\_yoto  
- /\<book-slug\>/\<title\>.jpg  
- Upload staging for browser file pickers: /tmp/libby\_to\_yoto/uploads/\<book-slug\>/

## 3\) Create the Yoto playlist

1. Log in to My Yoto.  
2. Create a new playlist from the My Playlists page by clicking the "Add Plalist" card.  
3. Set the playlist name to the book title.  
4. Upload the chunked audio files, not the original Libby parts. You can upload multiple files at once so upload all files in one batch. If you have to upload in multiple batches be sure that you upload the files in order.  
5. Verify the order matches the original audiobook and that there are no duplicates.  
6. Add the cover image as artwork.  
7. Save/create the playlist.

## 4\) Wrap up

1. close all browser tabs

## Local scripts

- prepare\_yoto.js — end-to-end prep from Libby parts to full file \+ title-prefixed chunks \+ manifest.  
- split\_audio.js — split-only helper for an already-stitched full audiobook file.

## Script usage

- End-to-end prep:  
  - node prepare\_yoto.js \--input-dir ./input \--output-dir ./output \--title "Any Book Title"  
- Split-only:  
  - node split\_audio.js \--input ./output/Any\_Book\_Title\_full.mp3 \--output-dir ./output \--title "Any Book Title"

## Cost / time reduction ideas

- Don’t upload Libby parts directly.  
- Split locally once.  
- Keep a persistent local folder per book.  
- Use a manifest so uploads are recoverable.  
- Avoid browser churn; keep the Libby and Yoto tabs open if possible.

