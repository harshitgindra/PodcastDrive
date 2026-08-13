# PodcastDrive

[![CI](https://github.com/harshitgindra/PodcastDrive/actions/workflows/test.yml/badge.svg)](https://github.com/harshitgindra/PodcastDrive/actions/workflows/test.yml)
[![Coverage](https://img.shields.io/badge/coverage-97%25-brightgreen)](https://github.com/harshitgindra/PodcastDrive/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/harshitgindra/PodcastDrive)

A self-hosted podcast pipeline that does two things:

1. **YouTube → Podcast** — Converts YouTube playlists and channels into podcast RSS feeds. Downloads audio as MP3, uploads to S3, and generates a podcast-compatible RSS 2.0 feed with iTunes extensions served via CloudFront.

2. **RSS Podcast → Clean Podcast** — Subscribes to existing RSS podcast feeds, downloads episodes, **removes ads automatically** using AWS Transcribe + Bedrock, and re-publishes cleaned episodes to your own CloudFront feed. Apple Podcasts / iTunes URLs are resolved automatically.

> ⚠️ **Personal use only.** This tool is intended solely for personal, non-commercial use. Downloading audio from YouTube may violate [YouTube's Terms of Service](https://www.youtube.com/t/terms). Only use this tool for content you have the legal right to download. See the [Disclaimer](#disclaimer) section for full details.

---

## How it works

### YouTube pipeline

1. Extracts all video IDs from a YouTube playlist (flat extraction, fast)
2. Compares against existing MP3s in S3 to find new videos
3. For each new video: downloads audio → converts to MP3 via FFmpeg → **removes ads** (AWS Transcribe + Bedrock + FFmpeg splice) → uploads to S3
4. Generates an RSS 2.0 feed with iTunes extensions and uploads it to S3
5. CloudFront serves the feed and MP3 files to podcast apps

### RSS Podcast pipeline

1. Reads podcast entries with `Source = Podcast` from the Notion DB (or YAML)
2. Resolves the feed URL:
   - **No URL set** → searches the iTunes Search API by podcast name and writes the discovered URL back to Notion
   - **Apple Podcasts / iTunes URL** → resolves it to the real RSS feed URL via the iTunes Lookup API and writes it back
3. Fetches the RSS feed and filters episodes by age (`MAX_AGE_DAYS`)
4. Diffs against existing episodes in S3 (by GUID-derived ID)
5. For each new episode: downloads MP3 → **removes ads** → uploads to S3
6. Generates and uploads a cleaned `feed.xml`
7. Updates Notion status and last-run timestamp

### Ad Removal

The ad-removal pipeline runs automatically for both YouTube and RSS episodes:

1. **Transcribe** — Uploads the MP3 to a temporary S3 prefix and submits it to AWS Transcribe for word-level transcription
2. **Detect** — Sends the transcript to AWS Bedrock (Claude Sonnet by default) to identify ad segments as `[{start, end}, ...]`
3. **Guard** — Segments longer than `MAX_AD_SEGMENT_SECS` (default 180 s) are silently dropped as false positives before any cutting happens
4. **Verify** — Segments longer than `AD_VERIFY_THRESHOLD_SECS` (default 90 s) trigger a second Bedrock call with supporting transcript context to confirm they are genuinely ads
5. **Snap** — When `AD_SNAP_TO_SILENCE=true` (default), each cut boundary is moved to the nearest silence gap within ±3 s, producing cleaner audio transitions
6. **Splice** — Uses FFmpeg `atrim` + `concat` to cut out confirmed ad segments and stitch the remaining audio into a clean MP3
7. **Fallback** — On any failure the original (unmodified) file is used so no episode is ever lost

Set `REMOVE_ADS=false` to disable ad removal entirely.

#### Ad removal evaluation (opt-in)

Set `EVALUATE_AD_REMOVAL=true` to enable post-clean quality checking. After each episode is cleaned, the evaluator re-runs Transcribe + Bedrock on the cleaned file and classifies any residual ads as:

- **`partial`** — residual within 10 s of an original segment boundary (trim miss — the cut landed slightly off)
- **`missed`** — residual far from all original segments (full detection gap)

A JSON report with improvement proposals is written to `reports/{slug}/{episode_id}_eval.json`. Evaluation failures never block the sync.

---

## Project structure

```
├── src/
│   ├── sync.py                 # YouTube pipeline orchestrator
│   ├── podcast_sync.py         # RSS podcast pipeline orchestrator
│   ├── podcast_downloader.py   # RSS feed fetcher, iTunes URL resolver, MP3 downloader
│   ├── ad_remover.py           # Ad removal (Transcribe → Bedrock → FFmpeg)
│   │                           #   detect_silence, snap_ad_boundaries, detect_ads,
│   │                           #   transcribe_audio, splice_audio, remove_ads
│   ├── ad_evaluator.py         # Ad removal quality evaluator (opt-in, EVALUATE_AD_REMOVAL=true)
│   ├── extractor.py            # YouTube playlist/video metadata extraction
│   ├── downloader.py           # YouTube audio download + FFmpeg MP3 conversion
│   ├── config_provider.py      # Podcast subscription config (YAML or Notion)
│   ├── rss_generator.py        # RSS 2.0 feed generation with iTunes tags (YouTube)
│   ├── s3_manager.py           # S3 operations and CloudFront invalidation
│   ├── preflight.py            # Startup checks (AWS credentials, FFmpeg, Transcribe, Bedrock)
│   ├── models.py               # Data models (PlaylistMeta, VideoEntry, EpisodeMeta)
│   ├── logger_config.py        # Logging setup (rotating file + console)
│   └── utils.py                # Utility functions (URL parsing, date parsing, AWS retry)
├── tests/                      # Unit test suite (684 tests, 97% overall coverage)
│   ├── test_ad_remover.py      # Ad removal pipeline — 113 tests, 96% coverage
│   ├── test_ad_evaluator.py    # Ad evaluator — 100% coverage
│   ├── test_ad_fixes.py        # Regression tests for the 5 ad-removal fixes
│   └── test_*.py               # All other module tests
├── eval/                       # Ad-cleaner manual test artefacts (git-ignored)
│   ├── transcripts/            # Cached Transcribe results keyed by episode ID
│   ├── episodes/               # Downloaded source MP3s for repeated testing
│   └── run_eval.py             # Batch evaluation runner
├── reports/                    # Ad-removal evaluation JSON reports (EVALUATE_AD_REMOVAL=true)
├── split_audiobook.sh          # One-command audiobook splitter (transcribe → detect chapters → split)
├── find_chapter_timestamps.py  # Transcribe an audiobook and detect chapter timestamps (CSV output)
├── split_audiobook.py          # Split an MP3 into chapters given a chapters.csv or embedded markers
├── test_ad_cleaner.py          # Manual end-to-end test harness (download → clean → listen)
├── test_ad.sh                  # One-command wrapper for test_ad_cleaner.py
├── requirements.txt            # Python dependencies
├── run.sh                      # Local run script (loads config.env, manages venv)
├── config.env.example          # Configuration template (copy to config.env)
├── podcasts.yaml.example       # Podcast subscriptions template (copy to podcasts.yaml)
├── Setup.md                    # macOS launchd scheduling guide
└── README.md
```

---

## S3 directory structure

```
s3://your-bucket/
  {playlist_id}/              # YouTube feed (playlist ID as prefix)
    feed.xml
    episodes/
      {video_id}.mp3

  {podcast-slug}/             # RSS podcast feed (name-derived slug as prefix)
    feed.xml
    episodes/
      {episode-id}.mp3
```

---

## Prerequisites

- Python 3.10+ (3.12+ recommended)
- FFmpeg (for MP3 conversion and ad splicing)
- AWS CLI v2 configured with credentials
- An S3 bucket
- A CloudFront distribution pointing to the S3 bucket
- *(For ad removal)* AWS Transcribe and Bedrock (Claude Sonnet or Amazon Nova Pro) enabled in your region

### Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Install FFmpeg

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

---

## Configuration

Copy `config.env.example` to `config.env` and fill in your values:

```bash
cp config.env.example config.env
```

### All environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `S3_BUCKET` | ✅ | — | S3 bucket name |
| `CLOUDFRONT_BASE` | ✅ | — | CloudFront base URL — no trailing slash (e.g. `https://abc.cloudfront.net`) |
| `CLOUDFRONT_DISTRIBUTION_ID` | | — | CloudFront distribution ID for cache invalidation after feed upload |
| `AWS_ACCESS_KEY_ID` | | — | AWS access key (or use IAM role / `~/.aws/credentials`) |
| `AWS_SECRET_ACCESS_KEY` | | — | AWS secret key |
| `AWS_DEFAULT_REGION` | | `us-west-2` | AWS region for all services |
| **YouTube** | | | |
| `MAX_DOWNLOADS_PER_RUN` | | `10` | Max new YouTube videos to download per run |
| `MAX_AGE_DAYS` | | `7` | Skip/delete episodes older than this many days |
| `SLEEP_BETWEEN_DOWNLOADS` | | `5` | Seconds to wait between downloads (rate-limit avoidance) |
| **RSS Podcasts** | | | |
| `PODCAST_MAX_EPISODES` | | `5` | Max new episodes to download per RSS podcast per run |
| `PODCAST_EPISODE_WORKERS` | | `1` | Number of episodes to process in parallel per podcast (download → transcribe → clean → upload). Set to `3` for a significant speed improvement — all steps are IO-bound so workers spend most time waiting, not competing for CPU. Higher values increase S3/Transcribe API concurrency. |
| **Ad Removal** | | | |
| `REMOVE_ADS` | | `true` | Set to `false` to disable ad removal entirely |
| `REMOVE_ADS_DRY_RUN` | | `false` | Set to `true` to detect and log ad segments without cutting the audio — useful for evaluating detection quality before enabling full removal |
| `TRANSCRIBE_LANGUAGE_CODE` | | `en-US` | BCP-47 language code for AWS Transcribe |
| `BEDROCK_MODEL_ID` | | `us.anthropic.claude-sonnet-4-6` | Bedrock model ID for **second-pass verification** of long segments — accuracy matters here, Sonnet recommended |
| `BEDROCK_DETECT_MODEL_ID` | | *(same as `BEDROCK_MODEL_ID`)* | Bedrock model ID for **first-pass detection** across all transcript chunks. Override with a cheaper model (e.g. Claude Haiku) to reduce per-episode detection cost by ~80% with minimal quality loss |
| `TRANSCRIBE_POLL_INTERVAL` | | `10` | Seconds between Transcribe job status polls |
| `TRANSCRIBE_MAX_WAIT` | | `3600` | Max seconds to wait for a Transcribe job before giving up |
| `MAX_AD_SEGMENT_SECS` | | `180` | Segments longer than this are treated as false positives and skipped — legitimate ads rarely exceed 3 minutes |
| `AD_VERIFY_THRESHOLD_SECS` | | `90` | Segments longer than this trigger a second Bedrock confirmation call before cutting — set to `0` to verify every segment, or a large number to skip verification |
| `AD_SNAP_TO_SILENCE` | | `true` | Snap cut boundaries to the nearest silence gap within ±3 s for cleaner audio transitions. Segment **starts** snap to the earlier candidate (avoids cutting into content); **ends** snap to the later candidate (avoids clipping the ad outro) |
| `SPLICE_LOUDNORM` | | `true` | Apply EBU R128 loudness normalisation (ffmpeg `loudnorm` filter) after splicing to equalise volume across all kept intervals. Set to `false` to skip (saves ~10–20% ffmpeg time) |
| `EVALUATE_AD_REMOVAL` | | `false` | Set to `true` to re-transcribe cleaned episodes and check for residual ads. Writes a JSON report to `reports/{slug}/{episode_id}_eval.json`. Disabled by default to avoid extra AWS costs. |
| `EVAL_REPORTS_DIR` | | `reports` | Local directory where ad-removal evaluation JSON reports are written |
| **Feed Customisation** | | | |
| `FEED_TITLE_SUFFIX` | | ` ✂️` | Suffix appended to the **channel** title in the RSS feed (e.g. "My Podcast ✂️"). Set to empty string to disable |
| `FEED_SUBTITLE` | | `Ad-free · PodcastDrive` | iTunes subtitle shown below the channel title in podcast apps |
| `EPISODE_AD_REMOVED_SUFFIX` | | ` ✂️` | Suffix appended to individual **episode** titles when ads were successfully removed. Helps distinguish cleaned episodes at a glance. Set to empty string to disable |
| **Config Provider** | | | |
| `CONFIG_PROVIDER` | | `yaml` | Config source: `yaml` or `notion` |
| `PODCASTS_YAML` | | `podcasts.yaml` | Path to YAML subscriptions file (when `CONFIG_PROVIDER=yaml`) |
| `NOTION_API_KEY` | | — | Notion integration token (when `CONFIG_PROVIDER=notion`) |
| `NOTION_DATABASE_ID` | | — | Notion database ID (when `CONFIG_PROVIDER=notion`) |
| **Logging** | | | |
| `LOG_DIR` | | `./logs` | Directory for rotating log files |
| `LOG_LEVEL` | | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_RETENTION_DAYS` | | `30` | Number of daily log files to keep |
| **Notifications** | | | |
| `HEALTH_ALERT_URL` | | — | Webhook URL for HIGH-priority health alerts (Slack, ntfy.sh, PagerDuty, etc.) |
| *(Herald)* | | — | Run-completion notifications are sent via [Herald](https://github.com/harshitgindra/Herald) if installed. No PodcastDrive config needed — Herald manages its own providers. |

---

## Running locally

### First-time setup

```bash
# 1. Configure environment
cp config.env.example config.env
# Edit config.env — set S3_BUCKET and CLOUDFRONT_BASE at minimum

# 2. Set up podcast subscriptions (YAML mode)
cp podcasts.yaml.example podcasts.yaml
# Edit podcasts.yaml — add YouTube playlists / channel handles
```

### Running

```bash
# Process all subscriptions from your config (podcasts.yaml or Notion)
./run.sh

# Dry-run — shows what would be downloaded/uploaded without making any changes
./run.sh --dry-run

# Process a specific YouTube playlist or channel directly
./run.sh PLyourPlaylistId
./run.sh @YourChannelHandle
./run.sh https://www.youtube.com/playlist?list=YOUR_PLAYLIST_ID

# Reprocess — delete episodes + ad caches, then re-sync with current settings
# Useful after tuning ad-detection parameters or upgrading the Bedrock model
./run.sh --reprocess all                  # all enabled podcasts
./run.sh --reprocess @YourChannelHandle   # one specific channel
./run.sh --reprocess PLyourPlaylistId     # one specific playlist

# Clear ad-segment caches (forces re-detection on next normal run, without re-downloading)
./run.sh --clear-cache all
./run.sh --clear-cache podcast-slug

# Reset — wipe ALL S3 data for enabled podcasts (destructive!)
./run.sh --reset                          # prompts for confirmation
./run.sh --reset --force                  # skip confirmation

# Show full usage help
./run.sh --help
```

`run.sh` handles:
- Loading `config.env`
- Creating / activating the Python virtual environment
- Installing dependencies if needed
- Running preflight checks (AWS credentials, FFmpeg, Transcribe, Bedrock)
- Processing all YouTube playlists (Source = YouTube)
- Processing all RSS podcast feeds (Source = Podcast)

### Example output

```json
{
  "playlist_id": "PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "new_episodes": 3,
  "skipped_old": 1,
  "failed": 0,
  "total_episodes": 12
}
```

The podcast feed is available at:
```
https://your-cloudfront-domain/{playlist_id}/feed.xml
```

---

## Testing the ad cleaner

`test_ad.sh` is a self-contained end-to-end test harness. It:

1. Bootstraps the virtual environment (creates it if needed)
2. Accepts any source — YouTube video/channel/playlist, RSS feed URL, or podcast name
3. Downloads the most-recent episode
4. Runs the full ad-removal pipeline (transcribe → detect → verify → snap → splice)
5. Re-transcribes the cleaned output and checks for residual ads
6. Retries from the original if residuals are found (up to `--max-iter` passes)
7. Prints a listening guide with timestamps for each removed segment

### Usage

```bash
# YouTube channel handle — fetches the latest upload
./test_ad.sh "@aliabdaal"

# YouTube video URL (specific episode)
./test_ad.sh "https://www.youtube.com/watch?v=LLjpnubsOWc"

# YouTube playlist ID
./test_ad.sh "PLEVkQGIATCXI1F2qs0slVE2MScaj1cSM0"

# RSS feed URL
./test_ad.sh "https://feeds.megaphone.fm/WWO4510910710"

# Podcast name — searches iTunes for the feed, downloads latest episode
./test_ad.sh "The Tim Ferriss Show"
```

### Options

| Option | Default | Description |
|---|---|---|
| `--skip-transcribe` | off | Reuse a cached transcript from `eval/transcripts/<episode_id>.json` — skips the Transcribe job and its cost. Useful for iterating on detection prompts. |
| `--max-iter N` | `2` | Maximum retry passes if residual ads are detected in the cleaned output. Each pass re-runs detection on the original audio with the residual timestamps as additional context. |
| `--no-snap` | off | Disable silence-boundary snapping. Cut boundaries stay exactly where Bedrock placed them. Useful for comparing snapped vs unsnapped output. |
| `--out-dir DIR` | `./test_output` | Directory where the cleaned MP3 and artefact JSON files are saved. |

### Example session

```
[TEST]   Source    : @aliabdaal
[TEST]   Episode   : The 5-Step Morning Routine (id=mEWanV5zrac)
[TEST]   Transcribe: cached (eval/transcripts/mEWanV5zrac.json)
[AD]     Detected 2 segment(s) — [118.4–243.1 s, 1847.2–1923.8 s]
[VERIFY] Segment 1: 118.4–243.1 s (124.7 s) → CONFIRMED (2nd-pass Bedrock)
[SNAP]   118.4 → 116.2 s (silence gap at 116.1–116.9 s)
[SNAP]   243.1 → 245.0 s (silence gap at 244.8–245.4 s)
[AD]     Splicing 2 ad segment(s) from mEWanV5zrac.mp3...
[TEST]   Saved: test_output/mEWanV5zrac_cleaned.mp3

── Listening Guide ─────────────────────────────────────────
  Original duration : 3847.2 s  (1:04:07)
  Cleaned duration  : 3612.4 s  (1:00:12)
  Segments removed  : 2

  Segment 1  1:56–4:03  (124.7 s)  CONFIRMED — verify cut at 1:55 and 4:05
  Segment 2  30:47–32:03  (76.6 s) — verify cut at 30:46 and 32:04

[EVAL]   Re-transcribing cleaned output...
[EVAL]   No residual ads found — clean ✓
```

### Transcript caching

The first run for any episode submits a Transcribe job (typically 2–5 minutes, billed per second). The result is saved to `eval/transcripts/<episode_id>.json`. Pass `--skip-transcribe` on subsequent runs to skip the job and reuse the cached transcript — iteration on prompts or snap parameters becomes effectively free.

---

## Splitting an audiobook

`split_audiobook.sh` is a one-command utility that transcribes an audiobook MP3, detects its chapter boundaries, and splits it into one file per chapter. It reuses the same AWS Transcribe + Bedrock infrastructure as the ad-removal pipeline.

### Usage

```bash
# Most common case — transcribe, detect chapters, split
./split_audiobook.sh audiobook.mp3

# Write chapter files to a specific directory
./split_audiobook.sh audiobook.mp3 --output-dir ~/Desktop/chapters

# Already have a chapters.csv? Skip transcription entirely
./split_audiobook.sh audiobook.mp3 --chapters chapters.csv

# Keyword detection missed chapters (e.g. untitled chapters) — go straight to Claude
./split_audiobook.sh audiobook.mp3 --bedrock-only

# Keep the raw timestamped transcript for reference or manual editing
./split_audiobook.sh audiobook.mp3 --save-transcript
```

### Options

| Option | Description |
|---|---|
| `--output-dir DIR` | Directory for split chapter files (default: `<name>_chapters/`) |
| `--chapters FILE` | Skip transcription and use an existing `chapters.csv` instead |
| `--bedrock-only` | Skip keyword detection; send the full transcript straight to Bedrock |
| `--save-transcript` | Save the raw timestamped transcript to `<name>_transcript.txt` |

### How it works

```
audiobook.mp3
  │
  ├─► AWS Transcribe ──► word-level timestamped transcript (cached in S3)
  │                                   │
  │                      Keyword pass — scan for "Chapter N / Part N /
  │                      Prologue / Epilogue" patterns (free, instant)
  │                                   │
  │                      < 2 chapters found?
  │                      └─► Bedrock (Claude) — semantic chapter detection
  │                          (auto-chunks transcripts longer than ~80 k chars)
  │                                   │
  │                      chapters.csv (title, start_time)
  │                                   │
  └─► FFmpeg -c copy ──► 01 - Chapter One.mp3
                         02 - Chapter Two.mp3
                         ...
```

The split uses `ffmpeg -c copy` (stream copy, no re-encode), so even a 20-hour audiobook splits in under a minute. AWS Transcribe results are cached in S3 — re-runs after the first transcription are instant and free.

### Manual chapters

If auto-detection does not produce the right boundaries, run with `--save-transcript` to get a timestamped `.txt` file, then create a `chapters.csv` by hand:

```csv
title,start_time
Introduction,00:00:00
Chapter 1 - The Beginning,00:05:30
Chapter 2 - Rising Action,00:52:11
Epilogue,08:14:00
```

Feed it back in with `--chapters chapters.csv` to skip transcription entirely.

---

## Ad removal detail

```
MP3 file
  │
  ├─► S3 upload (temp prefix)
  │         │
  │         └─► AWS Transcribe ──► word-level transcript
  │                                         │
  │                              AWS Bedrock (Claude Sonnet)
  │                              "identify ad segments"
  │                                         │
  │                              [{start, end}, ...]
  │                                         │
  │                         MAX_AD_SEGMENT_SECS guard (drop >180 s)
  │                                         │
  │                         AD_VERIFY_THRESHOLD_SECS check
  │                         ├─ short segment → keep
  │                         └─ long segment  → 2nd Bedrock call to confirm
  │                                         │
  │                         AD_SNAP_TO_SILENCE boundary adjustment (±3 s)
  │                                         │
  │                              FFmpeg atrim+concat
  │                                         │
  └─────────────────────────────────────────► cleaned MP3 (or original on failure)
```

### The five fixes

The following improvements were made to address known failure modes observed in production:

| # | Fix | Env var | Why it matters |
|---|---|---|---|
| 1 | **Duration guard** | `MAX_AD_SEGMENT_SECS=180` | Prevents false positives where Bedrock marks an entire content block (e.g. 341 s) as an ad |
| 2 | **Tighter merge gap** | Hard-coded 2 s (down from 5 s) | Avoids merging two distinct ads into one oversized segment that would then be verified |
| 3 | **Verification pass** | `AD_VERIFY_THRESHOLD_SECS=90` | For borderline long segments, a second Bedrock prompt with transcript context confirms or rejects the segment before cutting |
| 4 | **Silence snapping** | `AD_SNAP_TO_SILENCE=true` | Moves cut boundaries to genuine silence gaps so the splice is inaudible |
| 5 | **Coordinate translation** | Internal (evaluator) | After splicing, timestamps in the cleaned file differ from the originals; the evaluator now translates residual timestamps back to the original timeline before classifying them as partial/missed |

---

## Notion integration

Set `CONFIG_PROVIDER=notion` in `config.env` and provide `NOTION_API_KEY` and `NOTION_DATABASE_ID`.

A single Notion database can hold both YouTube and RSS podcast subscriptions — the `Source` column determines which pipeline handles each entry.

### Notion database schema

| Column | Notion type | Required | Description |
|---|---|---|---|
| `Name` | Title | ✅ | Display name for the podcast |
| `URL` | Rich Text or URL | | YouTube playlist ID / channel handle / full URL **or** RSS feed URL / Apple Podcasts URL. May be left blank for RSS podcasts — the feed URL will be auto-discovered via iTunes Search and written back. |
| `Source` | Select | ✅ | `YouTube` — processed by the YouTube pipeline. `Podcast` — processed by the RSS podcast pipeline. |
| `Enabled` | Checkbox | | Whether to include this podcast in the sync (default: checked) |
| `Max Downloads` | Number | | Per-run episode limit — overrides `MAX_DOWNLOADS_PER_RUN` / `PODCAST_MAX_EPISODES` |
| `Max Age Days` | Number | | Episode retention in days — overrides `MAX_AGE_DAYS` |
| `Status` | Select | | Written by the tool: `Running` → `Done` or `Failed` |
| `LastUpdated` | Date | | Written back by the tool after each successful sync |
| `Podcast URL` | URL | | Written back by the tool with the CloudFront RSS feed URL |

### Apple Podcasts / iTunes URL auto-resolution

If you paste an Apple Podcasts link (e.g. `https://podcasts.apple.com/us/podcast/name/id123456789`) into the `URL` field, PodcastDrive will:
1. Call the iTunes Lookup API to get the real RSS feed URL
2. Write the resolved URL back to the Notion `URL` field
3. Use the resolved URL for all subsequent runs (no repeated API calls)

If you leave `URL` blank for a `Source = Podcast` entry, PodcastDrive will:
1. Search the iTunes Search API by the podcast `Name`
2. Log the matched podcast name so you can verify it's correct
3. Write the discovered URL back to Notion

---

## Infrastructure & System Design

```
+------------------------------------------------------------------+
|                     CONFIGURATION SOURCES                        |
|                                                                  |
|   podcasts.yaml  --+                                             |
|                    +--> Config Provider --> Podcast list         |
|   Notion Database --    (YouTube / RSS Podcast)                  |
+------------------------------------------------------------------+
               |                          |
               v (Source=YouTube)         v (Source=Podcast)
+------------------------------+  +-------------------------------+
|  YOUTUBE PIPELINE            |  |  RSS PODCAST PIPELINE         |
|                              |  |                               |
|  yt-dlp (extract + download) |  |  iTunes API (URL resolution)  |
|  FFmpeg (MP3 conversion)     |  |  HTTP (fetch RSS + MP3)       |
|       |                      |  |       |                       |
|       v                      |  |       v                       |
|  Ad Removal Pipeline         |  |  Ad Removal Pipeline          |
|  (Transcribe→Bedrock→FFmpeg) |  |  (Transcribe→Bedrock→FFmpeg)  |
|       |                      |  |       |                       |
|       v                      |  |       v                       |
|  S3 upload + feed.xml        |  |  S3 upload + feed.xml         |
+------------------------------+  +-------------------------------+
               |                          |
               +----------+  +-----------+
                          v  v
+------------------------------------------------------------------+
|                        DELIVERY                                  |
|                                                                  |
|   S3 --> CloudFront CDN --> Podcast Apps (Overcast, etc.)        |
|                                                                  |
|   Notion write-back (Status, LastUpdated, Podcast URL)           |
+------------------------------------------------------------------+
```

---

## Running tests

```bash
source .venv/bin/activate

# Run all tests
python -m pytest tests/ --tb=short

# Run with full coverage report (CI threshold is 95%)
python -m pytest tests/ --cov=src --cov-report=term-missing --cov-fail-under=95

# Run a specific module
python -m pytest tests/test_podcast_sync.py -v
python -m pytest tests/test_ad_remover.py -v
```

Or use the Makefile shortcuts:

```bash
make test       # run full suite
make coverage   # run with coverage report (fails if < 95%)
```

Expected output:
```
657 passed, 9 skipped in ~12s

Name                        Stmts   Miss  Cover
-----------------------------------------------
src/ad_evaluator.py            92      0   100%
src/ad_remover.py             614     23    96%
src/s3_manager.py             156      0   100%
...
TOTAL                        2413     65    97%
```

CI runs the same command on Python 3.11, 3.12, and 3.13 via GitHub Actions.

### Test categories

| Test file | What it covers |
|---|---|
| `test_ad_remover.py` | All ad-removal functions: transcription polling, segment detection, silence snapping, splice logic, fallback behaviour |
| `test_ad_evaluator.py` | Residual classification, proposal generation, coordinate translation between cleaned and original timelines |
| `test_ad_fixes.py` | Regression tests for the 5 fixes: duration guard, merge threshold, verification pass, silence snapping, coordinate translation |
| `test_podcast_sync.py` | RSS podcast pipeline: feed fetching, episode diffing, sync orchestration |
| `test_podcast_downloader.py` | iTunes URL resolution, feed parsing, MP3 download |
| `test_sync.py` | YouTube pipeline orchestration |
| `test_s3_manager.py` | S3 upload/download, CloudFront invalidation |
| `test_integration.py` | End-to-end pipeline integration with mocked AWS |

---

## How cleanup works

Every run:
- **New episodes**: Downloaded if published within the last `MAX_AGE_DAYS` days and not already in S3
- **Existing episodes**: Kept in the feed even if they fall outside the current age window (S3 lifecycle rules handle eventual deletion)
- **YouTube orphans**: If a video is removed from the YouTube playlist, its MP3 is deleted from S3 on the next reconciliation pass

---

## Troubleshooting

### Ad removal skipped / falling back to original

Check the logs for `[AdRemover]` lines. Common causes:

- `REMOVE_ADS=false` — ad removal is explicitly disabled
- `S3_BUCKET` not set — required for the temporary Transcribe upload
- AWS Transcribe or Bedrock not enabled in your region
- Episode has no speech (music-only, very short clip)

In all cases the original unmodified episode is still uploaded and available.

Increase logging detail to see exactly which step failed:

```bash
LOG_LEVEL=DEBUG ./run.sh
```

Key log prefixes: `[AdRemover]`, `[Transcribe]`, `[Bedrock]`, `[Splice]`.

### Large segment detected and dropped as a false positive

If you see a log line like:

```
[AdRemover] Segment 118.4–459.7 s (341.3 s) exceeds MAX_AD_SEGMENT_SECS=180 — skipping
```

Bedrock flagged a content block (often an interview segment or long monologue) as an ad. This is the most common false-positive pattern for long-form podcasts.

**If the skipped segment was genuinely an ad:** lower `MAX_AD_SEGMENT_SECS`:
```bash
MAX_AD_SEGMENT_SECS=300 ./run.sh
```

**If segments are being skipped correctly but you want visibility into what was detected:** enable dry-run mode:
```bash
REMOVE_ADS_DRY_RUN=true ./run.sh
```

The log will show all detected segments without cutting any audio.

### Residual ads after cleaning

If you use `EVALUATE_AD_REMOVAL=true` and see residual segments in `reports/`:

```json
{
  "residuals": [
    {
      "start": 12.3,
      "end": 47.8,
      "classification": "missed",
      "note": "No original segment within 10 s"
    }
  ]
}
```

- **`partial`** residuals (close to an original boundary): the cut landed slightly before/after the real boundary. Try enabling silence snapping (`AD_SNAP_TO_SILENCE=true`) or running `test_ad.sh` with `--max-iter 3` to allow extra retry passes.
- **`missed`** residuals: Bedrock did not detect this ad at all on the first pass. Run `test_ad.sh` with the specific episode URL and inspect the listening guide to understand what was missed.

### Verification rejecting segments

If you see:
```
[AdRemover] Segment 92.0–198.5 s failed 2nd-pass verification — skipping
```

The second Bedrock call reviewed the transcript context around the segment and decided it was not an ad. This is the intended behaviour for borderline segments.

If you believe the segment was genuinely an ad: lower the verification threshold or disable verification for the specific run:
```bash
AD_VERIFY_THRESHOLD_SECS=999 ./run.sh
```

### Silence snapping producing unexpected cut points

If snapped boundaries jump too far from the original detection:

```
[AdRemover] Snap start 118.4 → 112.1 s (nearest silence at 111.8–113.0 s)
```

The ±3 s search window found a silence significantly away from the original boundary. This can happen when there is a long continuous-speech block with only a single distant silence.

Disable snapping for a specific run:
```bash
AD_SNAP_TO_SILENCE=false ./run.sh
```

Or use `test_ad.sh --no-snap` to compare snapped vs unsnapped output side-by-side before committing to a setting.

### Transcript cache reuse

`test_ad.sh` saves transcripts to `eval/transcripts/<episode_id>.json`. If you modify the detection prompt or pipeline logic, the cached transcript is still valid (it is the raw Transcribe output) — pass `--skip-transcribe` freely.

**If transcription itself changes** (e.g. language code, model update): delete the cached file and re-run without `--skip-transcribe`.

```bash
rm eval/transcripts/<episode_id>.json
./test_ad.sh "<source>"
```

### Multiple ad segments merged into one oversized segment

If Bedrock returns two adjacent ads that get merged into a segment that then fails the duration guard:

```
[AdRemover] Segment 120.0–310.0 s (190.0 s) exceeds MAX_AD_SEGMENT_SECS=180 — skipping
```

The merge gap threshold is 2 s. If the two ads have a gap of exactly 2 s or less between them, they are treated as one. You can:

1. Raise `MAX_AD_SEGMENT_SECS` so the merged segment passes the guard
2. Lower `AD_VERIFY_THRESHOLD_SECS` to trigger verification, which will re-examine the merged segment and may confirm it

### iTunes URL not resolving

If `search_feed_url_by_name` finds the wrong podcast, set the correct RSS feed URL directly in the Notion `URL` field. PodcastDrive will use it on the next run and skip the iTunes search.

### YouTube rate limiting

YouTube rate-limits aggressive scraping. The tool includes a configurable delay between downloads (`SLEEP_BETWEEN_DOWNLOADS`, default 5 seconds). If you see "rate-limited" errors, wait an hour and try again.

### launchd service exits immediately / yt-dlp not found

If the launchd service fails with `FileNotFoundError: [Errno 2] No such file or directory: 'yt-dlp'` (visible in `logs/launchd.stderr`), the service is launching with a restricted PATH that doesn't include the virtual environment.

Fix: add `.venv/bin` to the `PATH` in your plist (`~/Library/LaunchAgents/<your-label>.plist`):

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>PATH</key>
    <string>/path/to/PodcastDrive/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
</dict>
```

Replace `/path/to/PodcastDrive` with the absolute path to your project directory (e.g. the output of `pwd` when run from the project root), and `<your-label>` with the `Label` value from your plist.

Then reload the service:

```bash
launchctl unload ~/Library/LaunchAgents/<your-label>.plist
launchctl load   ~/Library/LaunchAgents/<your-label>.plist
launchctl start  <your-label>
```

Verify it is running (the PID column should be non-zero):

```bash
launchctl list | grep podcasts
```

### Format not available

If you see "Requested format is not available" errors, ensure yt-dlp is up to date: `pip install --upgrade yt-dlp`.

### Enabling debug logging

All log output is routed through Python's `logging` module. Set `LOG_LEVEL=DEBUG` to see every AWS API call, retry attempt, FFmpeg command, and segment decision:

```bash
LOG_LEVEL=DEBUG ./run.sh 2>&1 | tee debug.log
```

Key log prefixes to grep for:

| Prefix | What it shows |
|---|---|
| `[AdRemover]` | High-level ad removal decisions (segments found, skipped, verified, snapped) |
| `[Transcribe]` | Job submission, polling, completion |
| `[Bedrock]` | Model calls, token counts, raw responses |
| `[Splice]` | FFmpeg commands and output |
| `[AdEvaluator]` | Post-clean evaluation, residual classification |
| `[S3Manager]` | Upload/download operations |
| `[PodcastSync]` | RSS pipeline orchestration |
| `[Sync]` | YouTube pipeline orchestration |

---

## End-to-End Regression Tests

Unit tests (`make test`) run fully offline with mocked AWS. For regression
testing the full ad-removal pipeline against real audio, use the E2E harness.

### Running the Tests

```bash
# Tier 1 — fast (~30s, ~$0.02): uses cached transcripts, only calls Bedrock + ffmpeg
./eval/run_e2e_tests.sh

# Tier 2 — full pipeline (~10 min, ~$0.50/fixture): includes real AWS Transcribe
./eval/run_e2e_tests.sh --full

# Override quality thresholds
./eval/run_e2e_tests.sh --f1-threshold 0.80 --recall-threshold 0.75

# Re-accept current output as new ground truth (after intentional improvements)
./eval/run_e2e_tests.sh --update-gt
```

### Ground Truth Annotation (one-time setup)

```bash
# Step 1 — run detection + splice against cached transcripts
python eval/run_eval.py --splice --skip-transcribe

# Step 2 — listen to eval/results/*_cleaned.mp3 vs the originals
# Step 3 — generate ground_truth.json from the latest detection run
python eval/run_eval.py --update-ground-truth --skip-transcribe

# Step 4 — review eval/ground_truth.json, verify timestamps, fill in:
#   "ad_phrases":      words from each ad (must NOT appear in cleaned audio)
#   "content_phrases": words from content (must survive the cut)
# Step 5 — commit eval/ground_truth.json
```

### What a Failure Means

| Failure type | Likely cause |
|---|---|
| F1 below threshold | Model or prompt regression — ads no longer detected |
| Recall below threshold | Specific ads are being missed |
| Ad phrase found in cleaned transcript | Splice failed or segment boundary was wrong |
| Content phrase missing | Over-removal — content was cut along with an ad |
| Duration deviation exceeded | Boundary snapping or detection changed significantly |

**These tests do not run in CI** (they require AWS credentials and ~$1 per run).
Run them manually before shipping prompt changes, model upgrades, or splice logic changes.

---

## Contributing

Contributions, bug reports, and feature requests are welcome. Please open an issue or pull request on GitHub. By contributing, you agree that your contributions will be licensed under the same licence as this project.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor guide, including:

- **Local setup** and virtual environment instructions
- **Git hooks** — install a `pre-push` hook (`make install-hooks`) that runs the test suite locally before every push, giving you the same feedback as CI without waiting for GitHub Actions
- **GitHub branch protection** — server-side rules that enforce CI for every contributor regardless of local setup. All PRs must pass the `ci-gate` check (Python 3.11 + 3.12 + 3.13, coverage ≥ 95%) and receive a code-owner review before merging. To activate these rules on the repository run:
  ```bash
  GITHUB_TOKEN=ghp_... make protect
  ```
- **Code style**, **commit message convention**, and **PR workflow**

### Quick start for contributors

```bash
git clone https://github.com/harshitgindra/PodcastDrive.git
cd PodcastDrive
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
make install-hooks   # optional but recommended — blocks broken pushes locally
make test            # verify everything passes before your first commit
```

## Licence

This project is licensed under the terms of the [LICENSE](LICENSE) file included in this repository.

## Disclaimer

> ⚠️ **PodcastDrive is intended for personal, non-commercial use only.**

Please read this section carefully before using or sharing this tool.

### YouTube Terms of Service
Downloading, storing, or redistributing YouTube content may violate [YouTube's Terms of Service](https://www.youtube.com/t/terms) (specifically section 5.B, which prohibits downloading content without explicit permission). Use this tool responsibly and only for:
- Your own uploaded content
- Content licensed under Creative Commons or a similarly permissive licence
- Content where the rights holder has explicitly granted permission to download

### Copyright
All downloaded audio remains the intellectual property of the original creator. This tool does **not** grant any rights to the content. Do **not** redistribute, re-upload, publish, or share downloaded content without explicit permission from the rights holder.

### No Affiliation
PodcastDrive is an independent open-source project. It is **not** affiliated with, endorsed by, or sponsored by YouTube, Google LLC, Apple Inc., or any content creator.

### No Warranty
This software is provided **"as is"**, without warranty of any kind, express or implied. The authors accept no liability for any damages, data loss, account suspension, legal action, or other consequences arising from the use of this tool. **You are solely responsible** for ensuring your use complies with all applicable laws, platform terms, and content licences.
