"""YouTube Playlist to Podcast sync engine.

Orchestrates the full pipeline: extract playlist metadata, diff against
existing S3 state, download new episodes, generate RSS feed, and reconcile.
"""

import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

from downloader import DownloadError, download_and_convert
from extractor import extract_playlist, extract_video_metadata
from rss_generator import build_episode_metadata, generate_rss
from s3_manager import S3Manager
from utils import extract_playlist_id, parse_upload_date

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def process_playlist(
    playlist_url: str,
    max_downloads: int | None = None,
    max_age_days: int | None = None,
    sleep_between: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Process a single playlist: download new episodes, upload to S3, generate RSS.

    When *dry_run* is ``True`` the function performs all read-only steps
    (playlist extraction, S3 listing, candidate selection) and logs what
    *would* happen, but skips all write operations (download, S3 upload,
    feed generation, reconciliation).  The returned counters reflect the
    planned actions rather than completed ones.

    Args:
        playlist_url: Full YouTube playlist URL.
        max_downloads: Override for max downloads per run (None = use env/default).
        max_age_days: Override for max episode age in days (None = use env/default).
        sleep_between: Override for sleep between downloads (None = use env/default).
        dry_run: If ``True``, no files are downloaded or uploaded and S3 is
                 not modified.

    Returns:
        dict with playlist_id, new_episodes, skipped_old, failed, total_episodes.
    """
    # --- Configuration: per-podcast overrides > env vars > defaults ---
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        raise ValueError("S3_BUCKET environment variable must be set")
    cloudfront_base = os.environ.get("CLOUDFRONT_BASE", "")
    if not cloudfront_base:
        raise ValueError("CLOUDFRONT_BASE environment variable must be set")
    if max_downloads is None:
        max_downloads = int(os.environ.get("MAX_DOWNLOADS_PER_RUN", "10"))
    if max_age_days is None:
        max_age_days = int(os.environ.get("MAX_AGE_DAYS", "7"))
    if sleep_between is None:
        sleep_between = int(os.environ.get("SLEEP_BETWEEN_DOWNLOADS", "5"))

    # Ensure int types (Notion returns floats for numbers)
    max_downloads = int(max_downloads)
    max_age_days = int(max_age_days)
    sleep_between = int(sleep_between)

    if sleep_between < 0:
        raise ValueError(
            f"sleep_between must be >= 0, got {sleep_between}. "
            "Set SLEEP_BETWEEN_DOWNLOADS=0 to disable sleeping."
        )

    if dry_run:
        logger.info("[DRY-RUN] No files will be downloaded or uploaded.")

    logger.info(
        "[Config] max_downloads=%d  max_age_days=%d  sleep_between=%ds  dry_run=%s",
        max_downloads, max_age_days, sleep_between, dry_run,
    )

    playlist_id = extract_playlist_id(playlist_url)
    s3 = S3Manager(bucket=bucket, playlist_id=playlist_id)
    tmp_dir = f"/tmp/{playlist_id}"

    try:
        os.makedirs(tmp_dir, exist_ok=True)

        # --- Step 1: Flat-extract playlist ---
        logger.info("[Step 1] Extracting playlist for %s", playlist_id)
        playlist_meta, video_entries = extract_playlist(playlist_url)
        logger.info("[Step 1] Playlist has %d videos", len(video_entries))

        # --- Step 2: List existing episodes in S3 ---
        existing_keys = s3.list_existing_episodes()
        logger.info("[Step 2] Found %d existing episodes in S3", len(existing_keys))

        # --- Step 3: Build download candidates ---
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        candidates = []
        skipped_existing = 0
        skipped_no_duration = 0

        for v in video_entries:
            if len(candidates) >= max_downloads:
                break
            if v.video_id in existing_keys:
                skipped_existing += 1
                continue
            if not v.duration or v.duration <= 0:
                skipped_no_duration += 1
                logger.debug("[Step 3] Skipping %s — no duration (live stream)", v.video_id)
                continue
            candidates.append(v)

        logger.info(
            "[Step 3] %d candidates to download (skipped %d existing, %d live streams)",
            len(candidates), skipped_existing, skipped_no_duration,
        )
        for i, c in enumerate(candidates):
            logger.info("[Step 3]   %d. %s (duration=%ss)", i + 1, c.video_id, int(c.duration or 0))

        # --- Step 4: Download, upload, update feed after each ---
        new_count = 0
        skipped_old = 0
        failed_count = 0

        for i, video in enumerate(candidates):
            logger.info(
                "[Step 4] --- Processing %d/%d: %s ---",
                i + 1, len(candidates), video.video_id,
            )

            try:
                # Extract full metadata
                logger.info("[Step 4] Extracting metadata for %s", video.video_id)
                meta = extract_video_metadata(video.webpage_url)

                if meta:
                    video.upload_date = meta["upload_date"]
                    video.description = meta["description"]
                    video.title = meta.get("title") or video.title
                    if meta.get("thumbnail"):
                        video.thumbnail = meta["thumbnail"]
                    if meta.get("duration"):
                        video.duration = meta["duration"]

                    # Skip if older than max_age_days
                    pub_date = parse_upload_date(video.upload_date)
                    if pub_date < cutoff:
                        skipped_old += 1
                        logger.info(
                            "[Step 4] Skipping %s — published %s, older than %d days",
                            video.video_id, video.upload_date, max_age_days,
                        )
                        continue

                if dry_run:
                    logger.info(
                        "[DRY-RUN] Would download and upload %s: %s",
                        video.video_id, video.title,
                    )
                    new_count += 1
                    continue

                # Download audio
                logger.info("[Step 4] Downloading %s: %s", video.video_id, video.title)
                mp3_path = download_and_convert(
                    video.webpage_url, video.video_id, tmp_dir,
                )

                # Upload to S3
                logger.info("[Step 4] Uploading %s to S3", video.video_id)
                s3.upload_episode(mp3_path, video.video_id)
                os.remove(mp3_path)
                new_count += 1

                # Update feed after each successful download (atomic progress)
                logger.info("[Step 4] Updating feed.xml after %s", video.video_id)
                _rebuild_feed(s3, video_entries, cloudfront_base, playlist_id, playlist_meta)
                logger.info("[Step 4] Done %s (%d downloaded so far)", video.video_id, new_count)

                if sleep_between > 0 and (i + 1) < len(candidates):
                    time.sleep(sleep_between)

            except (DownloadError, Exception) as exc:
                failed_count += 1
                logger.error("[Step 4] FAILED %s: %s", video.video_id, exc)

        logger.info(
            "[Step 4] Download phase complete: %d new, %d skipped (old), %d failed",
            new_count, skipped_old, failed_count,
        )

        # --- Step 5: Reconciliation ---
        if dry_run:
            logger.info("[DRY-RUN] Skipping reconciliation and feed upload.")
            final_keys = existing_keys  # use pre-run S3 state for total count
        else:
            logger.info("[Step 5] Starting reconciliation...")
            _reconcile(
                s3, video_entries, cloudfront_base, playlist_id,
                playlist_meta, max_age_days,
            )
            final_keys = s3.list_existing_episodes()

        logger.info(
            "=== DONE === %d new, %d skipped_old, %d failed, %d total in S3",
            new_count, skipped_old, failed_count, len(final_keys),
        )

        return {
            "playlist_id": playlist_id,
            "new_episodes": new_count,
            "skipped_old": skipped_old,
            "failed": failed_count,
            "total_episodes": len(final_keys),
        }

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info("Cleaned up %s", tmp_dir)


def _rebuild_feed(
    s3: "S3Manager",
    video_entries: list,
    cloudfront_base: str,
    playlist_id: str,
    playlist_meta: "PlaylistMeta",
) -> int:
    """Re-list S3, generate and upload feed.xml using metadata already in memory."""
    final_keys = s3.list_existing_episodes()
    episodes = build_episode_metadata(
        video_entries, final_keys, cloudfront_base, playlist_id, s3
    )
    xml = generate_rss(playlist_meta, episodes, cloudfront_base, playlist_id)
    s3.upload_feed(xml)
    return len(episodes)


def _reconcile(
    s3: "S3Manager",
    video_entries: list,
    cloudfront_base: str,
    playlist_id: str,
    playlist_meta: "PlaylistMeta",
    max_age_days: int,
) -> None:
    """Reconcile S3 files and RSS feed entries.

    After this runs:
    - No entries older than max_age_days in the feed or S3
    - No feed entries without a corresponding S3 file
    - No S3 files without a corresponding feed entry
    - Entry count in feed == file count in S3
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    s3_keys = s3.list_existing_episodes()
    logger.info("[Reconcile] S3 has %d episodes", len(s3_keys))

    entry_map = {v.video_id: v for v in video_entries}

    # 1. Remove S3 files older than max_age_days
    to_delete_old = []
    for vid in s3_keys:
        entry = entry_map.get(vid)
        if entry and entry.upload_date:
            pub_date = parse_upload_date(entry.upload_date)
            if pub_date < cutoff:
                to_delete_old.append(vid)

    for vid in to_delete_old:
        logger.info("[Reconcile] Deleting old episode: %s", vid)
        try:
            s3.delete_episode(vid)
        except Exception as exc:
            logger.error("[Reconcile] Failed to delete %s: %s", vid, exc)

    if to_delete_old:
        logger.info("[Reconcile] Deleted %d old episodes", len(to_delete_old))

    # 2. Remove S3 files no longer in the playlist
    playlist_ids = {v.video_id for v in video_entries}
    orphaned_files = s3.list_existing_episodes() - playlist_ids
    for vid in orphaned_files:
        logger.info("[Reconcile] Deleting orphaned S3 file: %s", vid)
        try:
            s3.delete_episode(vid)
        except Exception as exc:
            logger.error("[Reconcile] Failed to delete orphan %s: %s", vid, exc)

    if orphaned_files:
        logger.info("[Reconcile] Deleted %d orphaned files", len(orphaned_files))

    # 3. Rebuild feed from current S3 state
    final_keys = s3.list_existing_episodes()
    episodes = build_episode_metadata(
        video_entries, final_keys, cloudfront_base, playlist_id, s3
    )
    xml = generate_rss(playlist_meta, episodes, cloudfront_base, playlist_id)
    s3.upload_feed(xml)

    logger.info(
        "[Reconcile] Done. Feed has %d entries, S3 has %d files",
        len(episodes), len(final_keys),
    )

    if len(episodes) != len(final_keys):
        logger.warning(
            "[Reconcile] MISMATCH: feed=%d, S3=%d",
            len(episodes), len(final_keys),
        )
