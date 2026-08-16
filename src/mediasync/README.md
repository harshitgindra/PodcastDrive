# MediaSync

On-demand YouTube media downloads to pCloud, driven by a shared Notion database.

## How it works

1. You paste a YouTube URL into the Notion database (via iOS Shortcut, web, or API)
2. MediaSync polls for pending entries, downloads audio/video, tags metadata, uploads to pCloud
3. CloudBeats (or any pCloud-connected player) sees the new files for offline playback

## Setup

1. Create a Notion integration and share your database with it
2. Create the Notion database with columns:
   - **URL** (url): YouTube link
   - **Profile** (select): profile name (e.g., "harshit", "spouse")
   - **Format** (select): "audio", "video", or "both"
   - **Status** (select): "pending" (set by default for new entries)
   - **Delete** (checkbox): check to remove from pCloud
   - **File Key** (rich_text): auto-filled after upload
   - **Duration** (number): auto-filled (seconds)
   - **Processed At** (date): auto-filled
   - **Error** (rich_text): auto-filled on failure
3. Copy `mediasync.env.example` → `mediasync.env` and fill in values
4. Run: `source mediasync.env && .venv/bin/python -m mediasync`

## Usage

```bash
# Full run
source mediasync.env && .venv/bin/python -m mediasync

# Dry-run (show pending without processing)
source mediasync.env && .venv/bin/python -m mediasync --dry-run

# Verbose output
source mediasync.env && .venv/bin/python -m mediasync -v
```

## Folder structure on pCloud

```
/MediaSync/
├── harshit/
│   ├── audio/
│   │   └── Song Title.m4a
│   └── video/
│       └── Tutorial.mp4
└── spouse/
    └── audio/
        └── Podcast Episode.m4a
```

## Deletion

Check the **Delete** checkbox in Notion. Next run will:
1. Delete the file(s) from pCloud
2. Archive the Notion row

## Deduplication

Same URL + same profile = skip. Different profiles can download the same URL independently.

## Herald integration

When `MEDIASYNC_HERALD_ENABLED=true`, sends a summary notification after each run.
Trigger via Herald: `/mediasync` command dispatches a run and replies with results.
