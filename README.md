# PodcastDrive

Converts YouTube playlists and channels into self-hosted podcast RSS feeds. Downloads audio as MP3, uploads to S3, and generates a podcast-compatible RSS 2.0 feed with iTunes extensions served via CloudFront.

Designed to run as an AWS Lambda function on a schedule, but can also be run locally.

> ⚠️ **Personal use only.** This tool is intended solely for personal, non-commercial use. Downloading audio from YouTube may violate [YouTube's Terms of Service](https://www.youtube.com/t/terms). Only use this tool for content you have the legal right to download. See the [Disclaimer](#disclaimer) section for full details.

## How it works

1. Extracts all video IDs from a YouTube playlist (flat extraction, fast)
2. Compares against existing MP3s in S3 to find new videos
3. For each new video: extracts full metadata, downloads audio, converts to MP3 via FFmpeg, uploads to S3
4. Removes episodes that are older than 7 days or no longer in the playlist
5. Generates an RSS 2.0 feed with iTunes extensions and uploads it to S3
6. CloudFront serves the feed and MP3 files to podcast apps

Each video is processed sequentially (download → upload → delete local file → next) to keep disk usage low.

## Project structure

```
├── src/                        # Core application code
│   ├── sync.py                 # Orchestrates the full pipeline
│   ├── extractor.py            # YouTube playlist/video metadata extraction
│   ├── downloader.py           # Audio download + FFmpeg MP3 conversion
│   ├── config_provider.py      # Podcast subscription config (YAML or Notion)
│   ├── rss_generator.py        # RSS 2.0 feed generation with iTunes tags
│   ├── s3_manager.py           # S3 operations and CloudFront invalidation
│   ├── models.py               # Data models (PlaylistMeta, VideoEntry, EpisodeMeta)
│   ├── logger_config.py        # Logging setup (rotating file + console)
│   └── utils.py                # Utility functions (URL parsing, date parsing)
├── tests/                      # Test suite
│   ├── conftest.py             # pytest configuration (adds src/ to path)
│   ├── test_utils.py           # Unit tests for utility functions
│   ├── test_downloader.py      # Unit tests for downloader
│   ├── test_extractor.py       # Unit tests for extractor
│   ├── test_rss_generator.py   # Unit tests for RSS generator
│   ├── test_s3_manager.py      # Unit tests for S3 manager
│   └── test_properties.py      # Property-based tests (Hypothesis)
├── requirements.txt            # Python dependencies
├── run.sh                      # Local run script (loads config.env, manages venv)
├── sync_podcast.py             # Alternative CLI entry point
├── config.env.example          # Configuration template (copy to config.env)
├── podcasts.yaml.example       # Podcast subscriptions template (copy to podcasts.yaml)
├── Setup.md                    # macOS launchd scheduling guide
└── README.md
```

## S3 directory structure

```
s3://your-bucket/
  {playlist_id}/
    feed.xml                    # RSS feed for this playlist
    episodes/
      {video_id}.mp3            # Audio files (named by video ID only)
```

Each playlist gets its own prefix. Video IDs are used as filenames to avoid exposing titles in URLs.

## Prerequisites

- Python 3.10+ (3.12+ recommended)
- FFmpeg (for MP3 conversion)
- AWS CLI v2 configured with credentials
- An S3 bucket
- A CloudFront distribution pointing to the S3 bucket

### Install Python dependencies

```bash
# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install all dependencies
pip install -r requirements.txt
```

### Install FFmpeg

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# Amazon Linux 2
sudo yum install ffmpeg
```

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `S3_BUCKET` | ✅ | — | S3 bucket name |
| `CLOUDFRONT_BASE` | ✅ | — | CloudFront distribution base URL (no trailing slash) |
| `CLOUDFRONT_DISTRIBUTION_ID` | | — | CloudFront distribution ID for cache invalidation |
| `AWS_ACCESS_KEY_ID` | | — | AWS access key (or use IAM role / `~/.aws/credentials`) |
| `AWS_SECRET_ACCESS_KEY` | | — | AWS secret key |
| `AWS_DEFAULT_REGION` | | `us-west-2` | AWS region |
| `MAX_DOWNLOADS_PER_RUN` | | `10` | Max new videos to download per invocation |
| `MAX_AGE_DAYS` | | `7` | Delete episodes older than this (by YouTube publish date) |
| `SLEEP_BETWEEN_DOWNLOADS` | | `5` | Seconds to wait between downloads (rate limit avoidance) |
| `CONFIG_PROVIDER` | | `yaml` | Config source: `yaml` or `notion` |
| `NOTION_API_KEY` | | — | Notion integration token (when `CONFIG_PROVIDER=notion`) |
| `NOTION_DATABASE_ID` | | — | Notion database ID (when `CONFIG_PROVIDER=notion`) |

Copy `config.env.example` to `config.env` and fill in your values:

```bash
cp config.env.example config.env
# Edit config.env with your S3 bucket name and CloudFront URL
```

## Running locally

### First-time setup

1. Configure your environment:

```bash
cp config.env.example config.env
# Edit config.env — set S3_BUCKET and CLOUDFRONT_BASE at minimum
```

2. Run the tool:

```bash
# Process all podcasts from your config (podcasts.yaml or Notion)
./run.sh

# Or process a specific playlist/channel directly
./run.sh PLyourPlaylistId
./run.sh @YourChannelHandle
./run.sh https://www.youtube.com/playlist?list=YOUR_PLAYLIST_ID
```

### Example output

```json
{
  "statusCode": 200,
  "playlist_id": "PLEVkQGIATCXI1F2qs0slVE2MScaj1cSM0",
  "new_episodes": 5,
  "cleaned_episodes": 0,
  "total_episodes": 17
}
```

The podcast feed is then available at:
```
https://your-cloudfront-domain/{playlist_id}/feed.xml
```

## Deploying to AWS Lambda

### Prerequisites

- AWS CLI v2 configured with credentials that can create IAM roles, Lambda functions, and Lambda layers

### Test the Lambda

```bash
aws lambda invoke \
  --function-name yt-podcast-lambda \
  --region us-west-2 \
  --payload '{"playlist_url": "https://www.youtube.com/playlist?list=YOUR_PLAYLIST_ID"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/response.json && cat /tmp/response.json
```

### Set up a schedule (EventBridge)

```bash
# Create a rule that runs every 6 hours
aws events put-rule \
  --name yt-podcast-sync \
  --schedule-expression 'rate(6 hours)' \
  --region us-west-2

# Add the Lambda as a target with your playlist URL
aws events put-targets \
  --rule yt-podcast-sync \
  --region us-west-2 \
  --targets '[{
    "Id": "my-playlist",
    "Arn": "arn:aws:lambda:us-west-2:ACCOUNT_ID:function:yt-podcast-lambda",
    "Input": "{\"playlist_url\": \"https://www.youtube.com/playlist?list=YOUR_PLAYLIST_ID\"}"
  }]'

# Grant EventBridge permission to invoke the Lambda
aws lambda add-permission \
  --function-name yt-podcast-lambda \
  --statement-id eventbridge-invoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-west-2:ACCOUNT_ID:rule/yt-podcast-sync \
  --region us-west-2
```

For multiple playlists, add more targets with different `Input` payloads. Each playlist produces its own independent feed.

## Multiple playlists

The same Lambda function handles multiple playlists. Each gets its own S3 prefix and feed URL:

```
https://your-cloudfront-domain/PLplaylistA/feed.xml
https://your-cloudfront-domain/PLplaylistB/feed.xml
```

Add separate EventBridge targets for each playlist, or invoke the Lambda manually with different `playlist_url` values.

## Running tests

```bash
source .venv/bin/activate
pip install -r requirements.txt

# Run all tests
python -m pytest tests/ -v

# Run just unit tests
python -m pytest tests/test_utils.py tests/test_downloader.py tests/test_extractor.py tests/test_rss_generator.py -v

# Run property-based tests
python -m pytest tests/test_properties.py -v
```

## How cleanup works

Every run:
- **New videos**: Downloaded if published within the last 7 days and not already in S3
- **Removed videos**: If a video is no longer in the YouTube playlist, its MP3 is deleted from S3
- **Old videos**: Videos older than 7 days (by YouTube publish date) are skipped during download and excluded from the feed. Existing MP3s for old videos are cleaned up when the feed is regenerated.

The 7-day window is based on the video's YouTube publish date, not when it was downloaded.

## Troubleshooting

### Rate limiting

YouTube rate-limits aggressive scraping. The tool includes a configurable delay between downloads (`SLEEP_BETWEEN_DOWNLOADS`, default 5 seconds). If you see "rate-limited" errors, wait an hour and try again.

### Format not available

If you see "Requested format is not available" errors, this usually means YouTube's SABR streaming is blocking format extraction. This is more common on AWS Lambda IPs. Running locally typically works. Ensure yt-dlp is up to date: `pip install --upgrade yt-dlp`.

### Lambda timeout

The Lambda has a 15-minute timeout. With `MAX_DOWNLOADS_PER_RUN=10` and ~30-60 seconds per video, this is usually sufficient. If you hit timeouts, reduce `MAX_DOWNLOADS_PER_RUN`. Remaining videos will be picked up on the next scheduled run.

## Infrastructure & System Design

### Overview

PodcastDrive is a serverless pipeline that converts YouTube playlists and channels into self-hosted podcast RSS feeds. Here's how the pieces fit together:

```
┌─────────────────────────────────────────────────────────────────┐
│                        CONFIGURATION SOURCES                    │
│                                                                 │
│   podcasts.yaml  ──┐                                            │
│                    ├──► Config Provider ──► List of Playlists   │
│   Notion Database ─┘                                            │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                        PROCESSING (Lambda / Local)              │
│                                                                 │
│   YouTube ──► yt-dlp (extract metadata + download audio)        │
│                  │                                              │
│                  ▼                                              │
│             FFmpeg (convert to MP3)                             │
│                  │                                              │
│                  ▼                                              │
│             S3 (upload MP3 + generate feed.xml)                 │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                        DELIVERY                                 │
│                                                                 │
│   S3 ──► CloudFront CDN ──► Podcast Apps (Overcast, etc.)       │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                        SCHEDULING (optional)                    │
│                                                                 │
│   AWS EventBridge (cron) ──► triggers Lambda on a schedule      │
└─────────────────────────────────────────────────────────────────┘
```

### Components

#### 1. Configuration Sources
Podcast subscriptions (which YouTube playlists/channels to follow) can be managed in two ways:

- **YAML file** (`podcasts.yaml`) — simple local file listing playlist IDs, channel handles (`@xyz`), or full URLs. Ideal for self-hosted or local runs.
- **Notion database** — a Notion DB with columns for Name, URL, Enabled, Max Downloads, and Max Age Days. Enables a no-code UI for managing subscriptions. The tool writes back the last-run timestamp and RSS feed URL to Notion after each sync.

#### 2. Processing Engine
The core pipeline runs either as an **AWS Lambda function** (scheduled) or **locally** via `run.sh`:

- **yt-dlp** — extracts playlist metadata and downloads audio streams from YouTube
- **FFmpeg** — converts the downloaded audio to MP3
- **Incremental sync** — compares existing S3 MP3 keys against the current playlist to find only new episodes, avoiding redundant downloads
- **Retention policy** — episodes older than `MAX_AGE_DAYS` (default: 7 days) are pruned from S3 automatically

#### 3. Storage (AWS S3)
All files are stored in a single S3 bucket, organised by playlist ID:

```
s3://your-bucket/
  {playlist_id}/
    feed.xml          ← RSS feed consumed by podcast apps
    episodes/
      {video_id}.mp3  ← Audio files
```

Each playlist is fully independent. The function is stateless — it derives all state from S3 key listings.

#### 4. CDN (AWS CloudFront)
A CloudFront distribution sits in front of the S3 bucket and serves all files publicly. After uploading a new `feed.xml`, the pipeline automatically:
- Creates a CloudFront cache invalidation for the feed file
- Pings the Overcast podcast app to trigger an immediate feed crawl

#### 5. Scheduling (AWS EventBridge)
An EventBridge cron rule invokes the Lambda function on a configurable schedule (e.g., every 6 hours). Each run picks up new episodes published since the last run.

### Data Flow (per playlist, per run)

```
1. Read podcast list from YAML / Notion
2. For each playlist:
   a. Fetch all video IDs from YouTube (flat extract, fast)
   b. List existing MP3s in S3
   c. Diff → new videos to download, stale videos to delete
   d. For each new video:
      - Extract full metadata (title, description, thumbnail, duration)
      - Download audio via yt-dlp
      - Convert to MP3 via FFmpeg
      - Upload to S3
      - Delete local temp file
   e. Delete stale/old MP3s from S3
   f. Generate RSS 2.0 feed XML (with iTunes extensions)
   g. Upload feed.xml to S3
   h. Invalidate CloudFront cache + ping Overcast
   i. Write back last-run timestamp to Notion (if using Notion provider)
```

## Architecture (simplified)

```
EventBridge (cron) → Lambda → yt-dlp → YouTube
                        ↓
                     FFmpeg (MP3 conversion)
                        ↓
                     S3 (MP3 files + feed.xml)
                        ↓
                     CloudFront → Podcast Apps
```

The Lambda function is stateless. It uses S3 prefix listing to determine what's already downloaded, so it can be safely re-run at any time.

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
PodcastDrive is an independent open-source project. It is **not** affiliated with, endorsed by, or sponsored by YouTube, Google LLC, or any content creator.

### No Warranty
This software is provided **"as is"**, without warranty of any kind, express or implied. The authors accept no liability for any damages, data loss, account suspension, legal action, or other consequences arising from the use of this tool. **You are solely responsible** for ensuring your use complies with all applicable laws, platform terms, and content licences.
