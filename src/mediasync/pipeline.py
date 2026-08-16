"""Main pipeline orchestration for MediaSync.

Stateless: reads pending work from Notion, downloads, uploads to S3,
updates Notion. Safe to run from any machine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from mediasync.config import Config
from mediasync.downloader import (
    DownloadError,
    DownloadResult,
    DurationExceededError,
    download,
)
from mediasync.notion_client import Format, MediaEntry, NotionClient, Status
from mediasync.s3_client import S3Client, S3Error
from mediasync.tagger import tag_file

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    """Counters for a single pipeline run."""

    processed: int = 0
    failed: int = 0
    deleted: int = 0
    skipped: int = 0


def run(config: Config) -> RunStats:
    """Execute one full pipeline cycle.

    1. Process soft-delete entries (remove from S3, archive in Notion).
    2. Process pending downloads (dedupe, download, tag, upload, update Notion).

    Returns:
        RunStats with counts of actions taken.
    """
    stats = RunStats()
    notion = NotionClient(config.notion_token, config.notion_database_id)
    s3 = S3Client(config.s3_bucket, config.s3_region)

    # Phase 1: deletions
    stats.deleted = _process_deletions(notion, s3)

    # Phase 2: pending downloads
    valid_profiles = {p.name for p in config.profiles}
    pending = notion.get_pending()
    logger.info("Found %d pending entries", len(pending))

    for entry in pending:
        if entry.profile not in valid_profiles:
            logger.warning("Unknown profile %r, skipping: %s", entry.profile, entry.url)
            stats.skipped += 1
            continue

        if _is_duplicate(notion, entry):
            logger.info("Duplicate URL for profile %s: %s", entry.profile, entry.url)
            notion.update_status(
                entry.page_id, Status.DONE, error="Skipped: duplicate URL"
            )
            stats.skipped += 1
            continue

        success = _process_entry(entry, notion, s3, config)
        if success:
            stats.processed += 1
        else:
            stats.failed += 1

    logger.info(
        "Run complete: processed=%d, failed=%d, deleted=%d, skipped=%d",
        stats.processed, stats.failed, stats.deleted, stats.skipped,
    )
    return stats


def _process_deletions(notion: NotionClient, s3: S3Client) -> int:
    """Delete files from S3 and archive Notion pages."""
    deletions = notion.get_deletions()
    count = 0
    for entry in deletions:
        try:
            if entry.file_key:
                # file_key may contain multiple keys separated by "; "
                for key in entry.file_key.split("; "):
                    if key.strip():
                        s3.delete_file(key.strip())
            notion.archive_page(entry.page_id)
            count += 1
        except S3Error as exc:
            logger.error("Failed to delete %s: %s", entry.file_key, exc)
    return count


def _is_duplicate(notion: NotionClient, entry: MediaEntry) -> bool:
    """Check if this URL already exists as 'done' for the same profile."""
    done = notion.get_done_for_profile(entry.profile)
    return any(d.url == entry.url for d in done)


def _process_entry(
    entry: MediaEntry,
    notion: NotionClient,
    s3: S3Client,
    config: Config,
) -> bool:
    """Download, tag, upload a single entry. Returns True on success."""
    notion.update_status(entry.page_id, Status.DOWNLOADING)

    try:
        results = download(
            entry.url,
            entry.format,
            output_dir=config.output_dir,
            max_duration_secs=config.max_duration_secs,
        )
    except DurationExceededError as exc:
        logger.warning("Duration exceeded: %s — %s", entry.url, exc)
        notion.update_status(entry.page_id, Status.FAILED, error=str(exc))
        return False
    except DownloadError as exc:
        logger.error("Download failed: %s — %s", entry.url, exc)
        notion.update_status(entry.page_id, Status.FAILED, error=str(exc))
        return False

    # Tag and upload
    file_keys: list[str] = []
    duration = 0
    try:
        for result in results:
            tag_file(result.path, result.title, result.artist)
            duration = result.duration_secs

            fmt_folder = "audio" if result.format_type == "audio" else "video"
            remote_folder = f"{config.s3_prefix}/{entry.profile}/{fmt_folder}"
            filename = result.path.name

            file_key = s3.upload(result.path, remote_folder, filename)
            file_keys.append(file_key)
    except S3Error as exc:
        logger.error("Upload failed: %s — %s", entry.url, exc)
        notion.update_status(entry.page_id, Status.FAILED, error=str(exc))
        return False
    finally:
        # Always clean up temp files
        for result in results:
            result.path.unlink(missing_ok=True)

    notion.update_status(
        entry.page_id,
        Status.DONE,
        file_key="; ".join(file_keys),
        duration=duration,
    )
    return True
