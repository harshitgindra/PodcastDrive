"""Main pipeline orchestration for MediaSync.

Stateless: reads pending work from Notion, downloads, uploads to storage,
updates Notion. Safe to run from any machine.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from mediasync.artwork import download_thumbnail
from mediasync.config import Config
from mediasync.url_handler import normalize_url
from concurrent.futures import ThreadPoolExecutor, as_completed

from mediasync.downloader import (
    DownloadError,
    DownloadResult,
    DurationExceededError,
    cache_metadata,
    cleanup_results,
    download,
    get_full_playlist_metadata,
    get_metadata,
    get_playlist_metadata,
    is_playlist,
)
from mediasync.notion_client import Format, MediaEntry, NotionClient, Status
from mediasync.playlist import generate_m3u, make_relative_keys
from mediasync.playlist_sync import sync_playlists
from mediasync.standing_playlists import generate_standing_playlists
from mediasync.storage import StorageBackend, create_storage
from mediasync.tagger import tag_file

logger = logging.getLogger(__name__)

# Track folders where artwork has been uploaded this run (avoid duplicates)
_uploaded_artwork: set[str] = set()


@dataclass
class RunStats:
    """Counters for a single pipeline run."""

    processed: int = 0
    failed: int = 0
    deleted: int = 0
    skipped: int = 0


def run(config: Config) -> RunStats:
    """Execute one full pipeline cycle.

    1. Process soft-delete entries (remove from storage, archive in Notion).
    2. Process pending downloads (dedupe, download, tag, upload, update Notion).

    Returns:
        RunStats with counts of actions taken.
    """
    stats = RunStats()
    _uploaded_artwork.clear()
    notion = NotionClient(config.notion_token, config.notion_database_id)
    storage = create_storage(config)

    # Phase 1: deletions
    stats.deleted = _process_deletions(notion, storage)

    # Phase 2: playlist sync — discover new items added to YouTube playlists
    profile_names = [p.name for p in config.profiles]
    new_from_playlists = sync_playlists(notion, profile_names)
    if new_from_playlists:
        logger.info("Playlist sync added %d new entries", new_from_playlists)

    # Phase 3: pending downloads
    valid_profiles = set(profile_names)
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

        success = _process_entry(entry, notion, storage, config)
        if success:
            stats.processed += 1
        else:
            stats.failed += 1

    # Phase 4: regenerate standing playlists if anything changed
    if stats.processed > 0 or stats.deleted > 0:
        generate_standing_playlists(config, notion, storage)

    logger.info(
        "Run complete: processed=%d, failed=%d, deleted=%d, skipped=%d",
        stats.processed, stats.failed, stats.deleted, stats.skipped,
    )
    return stats


def _process_deletions(notion: NotionClient, storage: StorageBackend) -> int:
    """Delete files from storage and archive Notion pages."""
    deletions = notion.get_deletions()
    count = 0
    for entry in deletions:
        try:
            if entry.file_key:
                for key in entry.file_key.split("; "):
                    if key.strip():
                        storage.delete_file(key.strip())
            notion.archive_page(entry.page_id)
            count += 1
        except Exception as exc:
            logger.error("Failed to delete %s: %s", entry.file_key, exc)
    return count


def _is_duplicate(notion: NotionClient, entry: MediaEntry) -> bool:
    """Check if this URL already exists as 'done' for the same profile."""
    done = notion.get_done_for_profile(entry.profile)
    return any(d.url == entry.url for d in done)


def _reconcile_with_storage(
    url: str,
    entry: MediaEntry,
    storage: StorageBackend,
    config: Config,
) -> tuple[list[str], int] | None:
    """Check if expected files already exist on storage.

    Fetches metadata from yt-dlp (no download) to determine the expected
    remote paths, then checks storage. For playlists, checks each item
    individually (fetches full metadata per item).

    Returns:
        (file_keys, total_duration) if all files exist, None otherwise.
    """
    if is_playlist(url):
        return _reconcile_playlist_with_storage(url, entry, storage, config)

    try:
        meta = get_metadata(url)
    except DownloadError:
        return None  # Can't reconcile without metadata; proceed to download

    title = _sanitize_title(meta.get("title", "untitled"))
    artist = meta.get("uploader") or meta.get("channel") or "Unknown"
    duration = int(meta.get("duration") or 0)

    formats_to_check: list[str] = []
    if entry.format in (Format.AUDIO, Format.BOTH):
        formats_to_check.append("audio")
    if entry.format in (Format.VIDEO, Format.BOTH):
        formats_to_check.append("video")

    file_keys: list[str] = []
    for fmt in formats_to_check:
        ext = "m4a" if fmt == "audio" else "mp4"
        fmt_folder = fmt
        if config.group_by_channel and artist and artist != "Unknown":
            channel = _sanitize_folder_name(artist)
            remote_folder = f"{config.prefix}/{entry.profile}/{fmt_folder}/{channel}"
        else:
            remote_folder = f"{config.prefix}/{entry.profile}/{fmt_folder}"
        remote_path = f"{remote_folder}/{title}.{ext}"

        if not storage.file_exists(remote_path):
            return None  # At least one file missing; need to download
        file_keys.append(remote_path)

    return file_keys, duration


def _reconcile_playlist_with_storage(
    url: str,
    entry: MediaEntry,
    storage: StorageBackend,
    config: Config,
) -> tuple[list[str], int] | None:
    """Check if all playlist items already exist on storage.

    Optimizations over naive per-item approach:
    1. Single yt-dlp call for full metadata (avoids N subprocess spawns)
    2. Folder listing (1-2 API calls) instead of per-file existence checks
    3. Caches metadata so subsequent download skips re-fetching
    4. Falls back to parallel per-item metadata if full extraction fails

    Returns:
        (file_keys, total_duration) if ALL items exist, None otherwise.
    """
    # Resolve full metadata for all playlist items
    items_meta = _fetch_playlist_items_metadata(url)
    if items_meta is None:
        return None

    logger.info("Reconciling playlist (%d items) against storage...", len(items_meta))

    formats_to_check: list[str] = []
    if entry.format in (Format.AUDIO, Format.BOTH):
        formats_to_check.append("audio")
    if entry.format in (Format.VIDEO, Format.BOTH):
        formats_to_check.append("video")

    # Pre-fetch folder listings (1 API call per unique folder vs N file_exists calls)
    folder_contents: dict[str, set[str]] = {}

    file_keys: list[str] = []
    total_duration = 0

    for idx, meta in enumerate(items_meta, 1):
        title = _sanitize_title(meta.get("title", "untitled"))
        artist = meta.get("uploader") or meta.get("channel") or "Unknown"
        duration = int(meta.get("duration") or 0)
        total_duration += duration

        for fmt in formats_to_check:
            ext = "m4a" if fmt == "audio" else "mp4"
            fmt_folder = fmt
            if config.group_by_channel and artist and artist != "Unknown":
                channel = _sanitize_folder_name(artist)
                remote_folder = f"{config.prefix}/{entry.profile}/{fmt_folder}/{channel}"
            else:
                remote_folder = f"{config.prefix}/{entry.profile}/{fmt_folder}"

            # Lazy-load folder listing
            if remote_folder not in folder_contents:
                try:
                    folder_contents[remote_folder] = storage.list_folder(remote_folder)
                except Exception:
                    folder_contents[remote_folder] = set()

            filename = f"{title}.{ext}"
            if filename not in folder_contents[remote_folder]:
                logger.info(
                    "Playlist item %d/%d not on storage: %s",
                    idx, len(items_meta), title,
                )
                return None  # At least one item missing; need full download
            file_keys.append(f"{remote_folder}/{filename}")

        if idx % 25 == 0:
            logger.info("Reconciliation progress: %d/%d items verified", idx, len(items_meta))

    logger.info("All %d playlist items already on storage", len(items_meta))
    return file_keys, total_duration


def _fetch_playlist_items_metadata(url: str) -> list[dict] | None:
    """Get full metadata for all playlist items, with caching.

    Strategy:
    1. Try single yt-dlp call (get_full_playlist_metadata) — fastest for large playlists
    2. Fall back to flat metadata + parallel per-item fetches if full extraction fails

    Caches each item's metadata for reuse during download.
    """
    # Strategy 1: single yt-dlp invocation for full metadata
    try:
        items = get_full_playlist_metadata(url)
        # Cache each item for potential later download
        for item in items:
            video_url = item.get("webpage_url") or item.get("url", "")
            if video_url:
                cache_metadata(video_url, item)
        return items
    except DownloadError:
        logger.debug("Full playlist metadata failed, falling back to parallel per-item")

    # Strategy 2: flat metadata (fast) + parallel per-item resolution
    try:
        flat_items = get_playlist_metadata(url)
    except DownloadError:
        return None

    if not flat_items:
        return None

    # Build video URLs from flat metadata
    video_urls: list[str] = []
    for item in flat_items:
        video_url = item.get("url") or item.get("webpage_url", "")
        if not video_url:
            video_id = item.get("id", "")
            if not video_id:
                return None
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        video_urls.append(video_url)

    # Parallel metadata fetches (8 workers: balances speed vs rate limits)
    results_meta: list[dict | None] = [None] * len(video_urls)

    def _fetch_one(idx_url: tuple[int, str]) -> tuple[int, dict]:
        idx, vurl = idx_url
        meta = get_metadata(vurl)  # Uses cache if available
        return idx, meta

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(_fetch_one, (i, u)): i
                for i, u in enumerate(video_urls)
            }
            for future in as_completed(futures):
                try:
                    idx, meta = future.result()
                    results_meta[idx] = meta
                    # Cache for later download
                    cache_metadata(video_urls[idx], meta)
                except DownloadError:
                    # One item failed; abort reconciliation
                    return None
    except Exception:
        return None

    # Filter out any None entries (shouldn't happen if we return None above)
    final = [m for m in results_meta if m is not None]
    if len(final) != len(video_urls):
        return None
    return final


def _sanitize_title(title: str) -> str:
    """Remove filesystem-unsafe characters from title (mirrors downloader logic)."""
    unsafe = '<>:"/\\|?*'
    result = title
    for ch in unsafe:
        result = result.replace(ch, "")
    result = " ".join(result.split())
    return result[:200]


def _process_entry(
    entry: MediaEntry,
    notion: NotionClient,
    storage: StorageBackend,
    config: Config,
) -> bool:
    """Download, tag, upload a single entry. Returns True on success."""
    url = normalize_url(entry.url)

    # Reconcile: check if files already exist on storage (skip download)
    reconciled = _reconcile_with_storage(url, entry, storage, config)
    if reconciled:
        file_keys, duration = reconciled
        logger.info("Already on storage, skipping download: %s", entry.url)
        notion.update_status(
            entry.page_id, Status.DONE,
            file_key="; ".join(file_keys),
            duration=duration,
        )
        return True

    notion.update_status(entry.page_id, Status.DOWNLOADING)

    try:
        results = download(
            url,
            entry.format,
            output_dir=config.output_dir,
            max_duration_secs=config.max_duration_secs,
            max_retries=config.max_retries,
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
    total_duration = 0
    total_items = len(results)
    try:
        for idx, result in enumerate(results, 1):
            # Progress feedback for multi-item downloads (playlists)
            if total_items > 1:
                notion.update_status(
                    entry.page_id, Status.DOWNLOADING,
                    error=f"Uploading {idx}/{total_items}: {result.title}",
                )

            tag_file(result.path, result.title, result.artist)
            total_duration += result.duration_secs

            fmt_folder = "audio" if result.format_type == "audio" else "video"
            if config.group_by_channel and result.artist and result.artist != "Unknown":
                channel = _sanitize_folder_name(result.artist)
                remote_folder = f"{config.prefix}/{entry.profile}/{fmt_folder}/{channel}"
            else:
                remote_folder = f"{config.prefix}/{entry.profile}/{fmt_folder}"
            filename = result.path.name

            file_key = storage.upload(result.path, remote_folder, filename)
            file_keys.append(file_key)

            # Upload folder artwork (once per unique remote_folder)
            if result.thumbnail_url and remote_folder not in _uploaded_artwork:
                _upload_folder_artwork(
                    result.thumbnail_url, remote_folder, storage, config.output_dir
                )
                _uploaded_artwork.add(remote_folder)

        # Generate and upload M3U playlist for playlist URLs with multiple items
        if is_playlist(url) and len(results) > 1:
            _upload_playlist(results, file_keys, entry, storage, config)

    except Exception as exc:
        logger.error("Upload failed: %s — %s", entry.url, exc)
        notion.update_status(entry.page_id, Status.FAILED, error=str(exc))
        return False
    finally:
        # Always clean up temp files
        cleanup_results(results)

    notion.update_status(
        entry.page_id,
        Status.DONE,
        file_key="; ".join(file_keys),
        duration=total_duration,
    )
    return True


def _upload_playlist(
    results: list[DownloadResult],
    file_keys: list[str],
    entry: MediaEntry,
    storage: StorageBackend,
    config: Config,
) -> None:
    """Generate an M3U playlist file and upload it for playlist downloads."""
    playlist_folder = f"{config.prefix}/{entry.profile}/playlists"

    # Build playlist items with relative paths
    relative_keys = make_relative_keys(playlist_folder, file_keys)

    items = []
    for result, rel_key in zip(results, relative_keys):
        items.append({
            "remote_key": rel_key,
            "title": result.title,
            "artist": result.artist,
            "duration_secs": result.duration_secs,
        })

    # Use the first result's title as a base for the playlist name
    # (yt-dlp metadata for playlists typically shares a common prefix)
    playlist_title = _derive_playlist_title(results)

    playlist_path = generate_m3u(playlist_title, items, config.output_dir)
    try:
        storage.upload(playlist_path, playlist_folder, playlist_path.name)
        logger.info("Uploaded playlist: %s/%s", playlist_folder, playlist_path.name)
    finally:
        playlist_path.unlink(missing_ok=True)


def _upload_folder_artwork(
    thumbnail_url: str,
    remote_folder: str,
    storage: StorageBackend,
    output_dir: str,
) -> None:
    """Download thumbnail and upload as folder.jpg for player artwork display."""
    thumb_path = download_thumbnail(thumbnail_url, output_dir)
    if thumb_path is None:
        return
    try:
        storage.upload(thumb_path, remote_folder, "folder.jpg")
        logger.debug("Uploaded folder artwork to %s", remote_folder)
    except Exception as exc:
        logger.warning("Failed to upload folder artwork (non-fatal): %s", exc)
    finally:
        thumb_path.unlink(missing_ok=True)


def _sanitize_folder_name(name: str) -> str:
    """Clean a channel/artist name for use as a folder name."""
    unsafe = '<>:"/\\|?*'
    result = name
    for ch in unsafe:
        result = result.replace(ch, "")
    # Collapse whitespace, trim dots/spaces (Windows compat)
    result = " ".join(result.split()).strip(". ")
    return result[:100] or "Unknown"


def _derive_playlist_title(results: list[DownloadResult]) -> str:
    """Derive a playlist title from the downloaded items.

    Uses the common prefix of all titles, falling back to the first title.
    """
    if not results:
        return "Playlist"

    titles = [r.title for r in results]
    if len(titles) == 1:
        return titles[0]

    # Find common prefix (word-aligned)
    prefix = os.path.commonprefix(titles).rstrip()
    # Only use prefix if it's meaningful (>5 chars)
    if len(prefix) > 5:
        return prefix.rstrip(" -–—|·")

    # Fallback: use the artist name if all same artist
    artists = {r.artist for r in results}
    if len(artists) == 1:
        return f"{next(iter(artists))} Playlist"

    return titles[0]
