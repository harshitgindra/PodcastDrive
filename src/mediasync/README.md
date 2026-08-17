# MediaSync

On-demand YouTube media downloads to cloud storage, driven by a shared Notion database.

## How it works

1. You paste a YouTube/Spotify/Apple Music URL into the Notion database (via iOS Shortcut, web, or API)
2. MediaSync polls for pending entries, downloads audio/video, embeds metadata + artwork, uploads to storage
3. CloudBeats (or any cloud-connected player) sees the new files organized by channel with playlists

## Setup

1. Create a Notion integration and share your database with it
2. Create the Notion database with columns:
   - **Name** (title): YouTube/Spotify/Apple Music URL (or text search query)
   - **Profile** (select): profile name (e.g., "harshit", "spouse")
   - **Format** (select): "audio", "video", or "both"
   - **Status** (select): "pending" (set by default for new entries)
   - **Priority** (number): optional — lower number = processed first
   - **Delete** (checkbox): check to remove from storage
   - **File Key** (rich_text): auto-filled after upload
   - **Duration** (number): auto-filled (total seconds)
   - **Processed At** (date): auto-filled
   - **Error** (rich_text): auto-filled on failure (or shows progress)
3. Copy `mediasync.env.example` -> `mediasync.env` and fill in values
4. Run: `./scripts/run_mediasync.sh`

## Usage


Pending downloads: 0

Pending deletions: 0
Storage backend: onedrive
FAIL: Token refresh failed — Token refresh failed: HTTP Error 400: Bad Request
The refresh token may have expired (90-day rolling window).
Re-run the OAuth flow to obtain a new token.

MediaSync Migration (DRY RUN)
========================================
This will:
  - Move files to channel-grouped folders
  - Upload folder.jpg artwork
  - Regenerate All/Recent playlists


MediaSync Migration (LIVE)
========================================
This will:
  - Move files to channel-grouped folders
  - Upload folder.jpg artwork
  - Regenerate All/Recent playlists

## Features

### Multi-platform URLs
- YouTube (videos + playlists)
- Spotify (tracks, albums, playlists — via yt-dlp extractors)
- Apple Music links
- Plain text (treated as YouTube search)

### Embedded metadata & artwork
Every downloaded file has embedded:
- Cover art (YouTube thumbnail)
- Title, artist, album (channel name), upload date

### Channel-grouped folders
Files are organized by uploader/channel for natural album-like browsing:


### Auto-generated playlists
- **All.m3u8**: every downloaded file for the profile
- **Recent.m3u8**: last 50 items
- **Per-playlist M3U**: auto-created when downloading a YouTube playlist

### Reliability
- Retry with exponential backoff (3 attempts) for transient failures
- Idempotent uploads (skips if file already exists remotely)
- Token health check and automatic refresh token rotation
- Progress feedback in Notion for long playlist downloads

### Deletion
Check the **Delete** checkbox in Notion. Next run will:
1. Delete the file(s) from storage
2. Archive the Notion row

### Deduplication
Same URL + same profile = skip. Different profiles can download the same URL independently.

### Priority
Set a number in the **Priority** column. Lower = processed first. Unset entries process in FIFO order.

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| MEDIASYNC_NOTION_TOKEN | Yes | — | Notion integration token |
| MEDIASYNC_NOTION_DATABASE_ID | Yes | — | Notion database ID |
| MEDIASYNC_PROFILES | Yes | — | Comma-separated profile names |
| MEDIASYNC_STORAGE | Yes | s3 | "s3" or "onedrive" |
| MEDIASYNC_S3_BUCKET | If s3 | — | S3 bucket name |
| MEDIASYNC_S3_REGION | No | us-west-2 | AWS region |
| MEDIASYNC_S3_PREFIX | No | MediaSync | S3 key prefix |
| MEDIASYNC_ONEDRIVE_CLIENT_ID | If onedrive | — | Azure app client ID |
| MEDIASYNC_ONEDRIVE_CLIENT_SECRET | If onedrive | — | Azure app client secret |
| MEDIASYNC_ONEDRIVE_REFRESH_TOKEN | If onedrive | — | OAuth2 refresh token |
| MEDIASYNC_ONEDRIVE_PREFIX | No | MediaSync | OneDrive folder prefix |
| MEDIASYNC_MAX_DURATION_SECS | No | 7200 | Max duration per video (seconds) |
| MEDIASYNC_MAX_RETRIES | No | 3 | Download retry attempts |
| MEDIASYNC_GROUP_BY_CHANNEL | No | true | Organize by channel folder |
| MEDIASYNC_OUTPUT_DIR | No | /tmp/mediasync | Temp download directory |
| MEDIASYNC_HERALD_ENABLED | No | true | Send notifications via Herald |

## Herald integration

When `MEDIASYNC_HERALD_ENABLED=true`, sends a summary notification after each run.
Trigger via Herald: `/mediasync` command dispatches a run and replies with results.
