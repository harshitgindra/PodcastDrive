# PodcastDrive

[![CI](https://github.com/harshitgindra/PodcastDrive/actions/workflows/test.yml/badge.svg)](https://github.com/harshitgindra/PodcastDrive/actions/workflows/test.yml)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)](https://github.com/harshitgindra/PodcastDrive/actions/workflows/test.yml)
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
2. **Detect** — Sends the transcript to AWS Bedrock (Amazon Nova Pro) to identify ad segments as `[{start, end}, ...]`
3. **Splice** — Uses FFmpeg `atrim` + `concat` to cut out ad segments and stitch the remaining audio into a clean MP3
4. **Fallback** — On any failure the original (unmodified) file is used so no episode is ever lost

Set `REMOVE_ADS=false` to disable ad removal entirely.

#### Ad removal evaluation (opt-in)

Set `EVALUATE_AD_REMOVAL=true` to enable post-clean quality checking. After each episode is cleaned, the evaluator re-runs Transcribe + Bedrock on the cleaned file and classifies any residual ads as:

- **`partial`** — residual within 10 s of an original segment boundary (trim miss)
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
│   ├── ad_evaluator.py         # Ad removal quality evaluator (opt-in, EVALUATE_AD_REMOVAL=true)
│   ├── extractor.py            # YouTube playlist/video metadata extraction
│   ├── downloader.py           # YouTube audio download + FFmpeg MP3 conversion
│   ├── config_provider.py      # Podcast subscription config (YAML or Notion)
│   ├── rss_generator.py        # RSS 2.0 feed generation with iTunes tags (YouTube)
│   ├── s3_manager.py           # S3 operations and CloudFront invalidation
│   ├── preflight.py            # Startup checks (AWS credentials, FFmpeg, Transcribe, Bedrock)
│   ├── models.py               # Data models (PlaylistMeta, VideoEntry, EpisodeMeta)
│   ├── logger_config.py        # Logging setup (rotating file + console)
│   └── utils.py                # Utility functions (URL parsing, date parsing)
├── tests/                      # Test suite (531 tests)
├── reports/                    # Ad-removal evaluation JSON reports (EVALUATE_AD_REMOVAL=true)
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
- *(For ad removal)* AWS Transcribe and Bedrock (Amazon Nova Pro) enabled in your region

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
| **Ad Removal** | | | |
| `REMOVE_ADS` | | `true` | Set to `false` to disable ad removal entirely |
| `TRANSCRIBE_LANGUAGE_CODE` | | `en-US` | BCP-47 language code for AWS Transcribe |
| `BEDROCK_MODEL_ID` | | `us.anthropic.claude-sonnet-4-20250514-v1:0` | Bedrock model ID for ad-segment detection |
| `TRANSCRIBE_POLL_INTERVAL` | | `10` | Seconds between Transcribe job status polls |
| `TRANSCRIBE_MAX_WAIT` | | `3600` | Max seconds to wait for a Transcribe job before giving up |
| `EVALUATE_AD_REMOVAL` | | `false` | Set to `true` to re-transcribe cleaned episodes and check for residual ads. Writes a JSON report to `reports/{slug}/{episode_id}_eval.json`. Disabled by default to avoid extra AWS costs. |
| `EVAL_REPORTS_DIR` | | `reports` | Local directory where ad-removal evaluation JSON reports are written |
| **Config Provider** | | | |
| `CONFIG_PROVIDER` | | `yaml` | Config source: `yaml` or `notion` |
| `PODCASTS_YAML` | | `podcasts.yaml` | Path to YAML subscriptions file (when `CONFIG_PROVIDER=yaml`) |
| `NOTION_API_KEY` | | — | Notion integration token (when `CONFIG_PROVIDER=notion`) |
| `NOTION_DATABASE_ID` | | — | Notion database ID (when `CONFIG_PROVIDER=notion`) |
| **Logging** | | | |
| `LOG_DIR` | | `./logs` | Directory for rotating log files |
| `LOG_LEVEL` | | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_RETENTION_DAYS` | | `30` | Number of daily log files to keep |

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

### Ad removal detail

```
MP3 file
  │
  ├─► S3 upload (temp prefix)
  │         │
  │         └─► AWS Transcribe ──► word-level transcript
  │                                         │
  │                              AWS Bedrock (Nova Pro)
  │                              "identify ad segments"
  │                                         │
  │                              [{start, end}, ...]
  │                                         │
  │                              FFmpeg atrim+concat
  │                                         │
  └─────────────────────────────────────────► cleaned MP3 (or original on failure)
```

---

## Running tests

```bash
source .venv/bin/activate

# Run all tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=src --cov-report=term-missing

# Run a specific module
python -m pytest tests/test_podcast_sync.py -v
python -m pytest tests/test_ad_remover.py -v
```

---

## How cleanup works

Every run:
- **New episodes**: Downloaded if published within the last `MAX_AGE_DAYS` days and not already in S3
- **Existing episodes**: Kept in the feed even if they fall outside the current age window (S3 lifecycle rules handle eventual deletion)
- **YouTube orphans**: If a video is removed from the YouTube playlist, its MP3 is deleted from S3 on the next reconciliation pass

---

## Troubleshooting

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

### Ad removal skipped / falling back to original

Check the logs for `[AdRemover]` lines. Common causes:
- `REMOVE_ADS=false` — ad removal is explicitly disabled
- `S3_BUCKET` not set — required for the temporary Transcribe upload
- AWS Transcribe or Bedrock not enabled in your region
- Episode has no speech (music-only, very short clip)

In all cases the original unmodified episode is still uploaded and available.

### iTunes URL not resolving

If `search_feed_url_by_name` finds the wrong podcast, set the correct RSS feed URL directly in the Notion `URL` field. PodcastDrive will use it on the next run and skip the iTunes search.

---

## Contributing

Contributions, bug reports, and feature requests are welcome. Please open an issue or pull request on GitHub. By contributing, you agree that your contributions will be licensed under the same licence as this project.

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
