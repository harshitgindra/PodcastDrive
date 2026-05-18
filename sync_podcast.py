#!/usr/bin/env -S .venv/bin/python3
"""YouTube Playlist to Podcast — Local sync tool.

Downloads new episodes as MP3, uploads to S3, generates RSS feed,
and cleans up stale episodes. Designed to run locally on a schedule
(e.g., via cron or launchd).

Usage:
    ./sync_podcast.py <playlist_url>
    ./sync_podcast.py https://www.youtube.com/playlist?list=PLEVkQGIATCXI1F2qs0slVE2MScaj1cSM0
"""

import argparse
import logging
import os
import shutil
import sys
import tempfile

# Add src/ to the Python path so we can import the modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from downloader import DownloadError, download_and_convert
from extractor import extract_playlist
from logger_config import setup_logging
from rss_generator import build_episode_metadata, generate_rss
from s3_manager import S3Manager
from utils import extract_playlist_id

# Initialise logging once at startup.
# Defaults can be overridden via LOG_DIR, LOG_LEVEL, LOG_RETENTION_DAYS env vars.
def _safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

setup_logging(
    log_dir=os.environ.get("LOG_DIR", os.path.join(os.path.dirname(__file__), "logs")),
    log_level=os.environ.get("LOG_LEVEL", "INFO"),
    retention_days=_safe_int(os.environ.get("LOG_RETENTION_DAYS", "30"), 30),
)
logger = logging.getLogger(__name__)

# Defaults (bucket and CloudFront must be supplied via args or config.env)
DEFAULT_BUCKET = os.environ.get("S3_BUCKET", "")
DEFAULT_CLOUDFRONT = os.environ.get("CLOUDFRONT_BASE", "")
DEFAULT_MAX_AGE = int(os.environ.get("MAX_AGE_DAYS", "7"))
DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")


def sync_playlist(
    playlist_url: str,
    bucket: str,
    cloudfront_base: str,
    max_age_days: int,
    region: str,
    dry_run: bool = False,
) -> dict:
    """Run the full sync pipeline for a single playlist.

    Args:
        playlist_url: YouTube playlist URL.
        bucket: S3 bucket name.
        cloudfront_base: CloudFront base URL.
        max_age_days: Maximum episode age in days.
        region: AWS region string.
        dry_run: If ``True``, perform all read-only steps and log planned
                 actions without downloading, uploading, or deleting anything.

    Returns:
        dict with playlist_id, new_episodes, cleaned_episodes,
        total_episodes, feed_url.
    """

    playlist_id = extract_playlist_id(playlist_url)
    logger.info("Syncing playlist: %s", playlist_id)

    # Use a temp directory for downloads
    tmp_dir = tempfile.mkdtemp(prefix=f"podcast-{playlist_id}-")

    s3 = S3Manager(bucket=bucket, playlist_id=playlist_id)

    try:
        if dry_run:
            logger.info("[DRY-RUN] No files will be downloaded, uploaded, or deleted.")

        # Step 1: Extract playlist metadata
        logger.info("Extracting playlist metadata...")
        playlist_meta, video_entries = extract_playlist(playlist_url)
        logger.info("Found %d videos in playlist", len(video_entries))

        # Step 2: List existing episodes in S3
        existing_keys = s3.list_existing_episodes()
        logger.info("Found %d existing episodes in S3", len(existing_keys))

        # Step 3: Diff — find new videos to download and stale ones to delete
        from datetime import datetime, timedelta, timezone
        from utils import parse_upload_date

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        playlist_video_ids = {v.video_id for v in video_entries}
        to_download = [v for v in video_entries if v.video_id not in existing_keys]
        to_delete = [vid for vid in existing_keys if vid not in playlist_video_ids]
        logger.info(
            "%d new to download, %d stale to delete",
            len(to_download), len(to_delete),
        )

        # Step 4: Download and upload new episodes
        new_count = 0
        for i, video in enumerate(to_download, 1):
            if dry_run:
                logger.info(
                    "[DRY-RUN] Would download: %s — %s",
                    video.video_id, video.title,
                )
                new_count += 1
                continue
            try:
                logger.info(
                    "[%d/%d] Downloading: %s — %s",
                    i, len(to_download), video.video_id, video.title,
                )
                mp3_path = download_and_convert(
                    video.webpage_url, video.video_id, tmp_dir
                )
                logger.info("Uploading %s to S3...", video.video_id)
                s3.upload_episode(mp3_path, video.video_id)
                os.remove(mp3_path)
                new_count += 1
            except (DownloadError, Exception) as exc:
                logger.error("Failed %s: %s", video.video_id, exc)

        # Step 5: Delete stale episodes
        cleaned_count = 0
        for video_id in to_delete:
            if dry_run:
                logger.info("[DRY-RUN] Would delete stale: %s", video_id)
                cleaned_count += 1
                continue
            try:
                logger.info("Deleting stale: %s", video_id)
                s3.delete_episode(video_id)
                cleaned_count += 1
            except Exception as exc:
                logger.error("Failed to delete %s: %s", video_id, exc)

        # Step 6: Re-list S3 and generate RSS
        if dry_run:
            logger.info("[DRY-RUN] Skipping RSS feed upload.")
            final_keys = existing_keys  # use pre-run S3 state
        else:
            final_keys = s3.list_existing_episodes()
            episodes_for_feed = build_episode_metadata(
                video_entries, final_keys, cloudfront_base, playlist_id, s3
            )
            rss_xml = generate_rss(
                playlist_meta, episodes_for_feed, cloudfront_base, playlist_id
            )
            s3.upload_feed(rss_xml)

        result = {
            "playlist_id": playlist_id,
            "new_episodes": new_count,
            "cleaned_episodes": cleaned_count,
            "total_episodes": len(final_keys),
            "feed_url": f"{cloudfront_base}/{playlist_id}/feed.xml",
        }

        logger.info(
            "Done: %d new, %d cleaned, %d total",
            new_count, cleaned_count, len(final_keys),
        )
        logger.info("Feed: %s", result["feed_url"])

        return result

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description="Sync a YouTube playlist to a podcast RSS feed on S3."
    )
    parser.add_argument(
        "playlist_url",
        help="YouTube playlist URL",
    )
    parser.add_argument(
        "--bucket", default=DEFAULT_BUCKET,
        help=f"S3 bucket name (default: {DEFAULT_BUCKET})",
    )
    parser.add_argument(
        "--cloudfront", default=DEFAULT_CLOUDFRONT,
        help=f"CloudFront base URL (default: {DEFAULT_CLOUDFRONT})",
    )
    parser.add_argument(
        "--max-age", type=int, default=DEFAULT_MAX_AGE,
        help=f"Max episode age in days (default: {DEFAULT_MAX_AGE})",
    )
    parser.add_argument(
        "--region", default=DEFAULT_REGION,
        help=f"AWS region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help=(
            "Preview what would be downloaded/deleted without making any changes. "
            "S3 is not modified and no files are downloaded."
        ),
    )

    args = parser.parse_args()

    result = sync_playlist(
        playlist_url=args.playlist_url,
        bucket=args.bucket,
        cloudfront_base=args.cloudfront,
        max_age_days=args.max_age,
        region=args.region,
        dry_run=args.dry_run,
    )

    print(f"\n{'='*50}")
    print(f"Playlist:  {result['playlist_id']}")
    print(f"New:       {result['new_episodes']}")
    print(f"Cleaned:   {result['cleaned_episodes']}")
    print(f"Total:     {result['total_episodes']}")
    print(f"Feed URL:  {result['feed_url']}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
