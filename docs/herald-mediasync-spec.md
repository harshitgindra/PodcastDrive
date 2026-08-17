# Herald Integration Spec: MediaSync Service

## Overview

MediaSync is a YouTube-to-OneDrive pipeline triggered via Telegram through Herald.
It downloads YouTube videos/playlists (as audio or video), tags them with metadata,
uploads to OneDrive, and tracks state in a Notion database.

## Herald Config Addition

Add this block under `listen.services` in `~/.config/herald/config.yaml`:

```yaml
    mediasync:
      aliases: [ms]
      description: "YouTube to OneDrive media sync"
      cwd: "/Users/hgindra/Projects/PodcastDrive"
      env:
        PATH: "/Users/hgindra/Projects/PodcastDrive/.venv/bin:/Users/hgindra/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        HOME: "/Users/hgindra"
      actions:
        sync:
          run: ["./scripts/run_mediasync.sh"]
          timeout: 3600
          description: "Run the full pipeline (process all pending Notion entries)"
          modifiers:
            reset:
              args: ["--reset"]
              description: "Reset all done/failed entries to pending and re-process"
              timeout: 7200
            verbose:
              args: ["-v"]
              aliases: [v]
            dry-run:
              args: ["--dry-run"]
              aliases: [dryrun, dry_run, dry]
              timeout: 60
              description: "Show pending entries without downloading"
```

## Telegram Commands

Once configured, the following commands work from Telegram:

| Command | What it does |
|---------|-------------|
| `/ms sync` | Run pipeline: download all pending Notion entries, upload to OneDrive |
| `/mediasync sync` | Same as above (full name) |
| `/ms sync dry-run` | Show what would be processed without downloading |
| `/ms sync reset` | Clear all done/failed statuses, then re-download everything |
| `/ms sync verbose` | Run with debug logging |
| `/ms sync reset verbose` | Reset + verbose (modifiers can be combined) |

## How the Pipeline Works

1. **Source of truth**: Notion database `3be27b92916c80eaa941d1f770f6b19d`
2. **Adding work**: Create a row in Notion with:
   - **Name** (title column): the YouTube URL (video or playlist)
   - **Profile**: select "Harshit" or "Dishita" (determines OneDrive subfolder)
   - **Format**: select "Audio", "Video", or "Both"
   - **Status**: leave blank (blank = pending)
   - **Delete**: unchecked
3. **Pipeline runs** (`/ms sync`):
   - Fetches all rows where Status is blank/pending and Delete is unchecked
   - For each entry: downloads via yt-dlp, tags with mutagen, uploads to OneDrive
   - Playlists are auto-detected and all items are downloaded
   - Updates Notion: Status → "done", File Key → remote path(s), Duration, Processed At
   - On failure: Status → "failed", Error → reason
4. **Deletion**: Check the "Delete" checkbox in Notion, then run sync — file is removed from OneDrive and Notion row is archived
5. **Reset** (`/ms sync reset`): Clears Status/File Key/Duration/Error/Processed At on all done/failed rows, then runs the pipeline from scratch

## File Storage Layout

Files are uploaded to OneDrive at:
```
OneDrive / MediaSync / {Profile} / {audio|video} / {filename}
```

Examples:
- `MediaSync/Harshit/audio/Song Title.m4a`
- `MediaSync/Dishita/video/Clip Name.mp4`
- Playlists: each track is a separate file, all keys joined by "; " in Notion

## Environment

All secrets are in `/Users/hgindra/Projects/PodcastDrive/mediasync.env` (sourced by the run script):

| Variable | Purpose |
|----------|---------|
| `MEDIASYNC_NOTION_TOKEN` | Notion API token |
| `MEDIASYNC_NOTION_DATABASE_ID` | Notion database ID |
| `MEDIASYNC_STORAGE` | Backend: `onedrive` |
| `MEDIASYNC_PROFILES` | Comma-separated: `Harshit,Dishita` |
| `MEDIASYNC_ONEDRIVE_CLIENT_ID` | Azure app client ID |
| `MEDIASYNC_ONEDRIVE_CLIENT_SECRET` | Azure app client secret |
| `MEDIASYNC_ONEDRIVE_REFRESH_TOKEN` | OAuth2 refresh token (90-day rolling) |

## Dependencies

- Python 3.13 virtualenv at `/Users/hgindra/Projects/PodcastDrive/.venv/`
- `yt-dlp` and `ffmpeg` in the venv/PATH
- `cookies.txt` in project root (for YouTube auth, refreshed separately)
- `deno` available at `/opt/homebrew/bin/deno` (YouTube n-challenge solver)
- Herald installed at `~/.local/bin/herald`

## Error Handling

- Individual download failures mark that Notion entry as "failed" but do not stop the pipeline
- For playlists: items exceeding `MEDIASYNC_MAX_DURATION_SECS` (7200s default) are skipped
- 403 errors mean cookies.txt is stale — run `./scripts/refresh_cookies.sh`
- OneDrive token refresh is automatic (access tokens expire hourly; refresh token valid 90 days)

## Notifications

After each run, Herald sends a Telegram summary:
```
MediaSync — 2m 15s
  Processed: 3
  Failed: 0
  Deleted: 1
  Skipped: 0
```

This happens via the built-in Herald notify at the end of the CLI (`--job mediasync`).

## Implementation Notes

- The run script (`scripts/run_mediasync.sh`) sources env, sets PATH, and execs `python -m mediasync "$@"`
- `--no-playlist` is used for individual video downloads; playlist detection uses `is_playlist()` which checks for `/playlist?list=` URLs
- Videos with `?v=X&list=Y` are treated as single videos (not playlists)
- The `DANGEROUS_FLAGS` list in Herald services.py includes `--reset` — Herald will warn during `--check` but this is intentional; acknowledge it
