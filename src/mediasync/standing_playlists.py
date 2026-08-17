"""Auto-generated standing playlists for each profile.

Generates \"All\" and \"Recent\" M3U playlists from existing done entries
in Notion, uploaded alongside the regular media files. CloudBeats can
import these for quick access without browsing folders.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from mediasync.config import Config
from mediasync.notion_client import NotionClient, Status
from mediasync.playlist import generate_m3u, make_relative_keys
from mediasync.storage import StorageBackend, create_storage

logger = logging.getLogger(__name__)

# Default: "Recent" includes the last 50 items
DEFAULT_RECENT_COUNT = 50


def generate_standing_playlists(
    config: Config,
    notion: NotionClient,
    storage: StorageBackend,
) -> int:
    """Generate and upload standing playlists for all profiles.

    Creates per-profile:
        - All.m3u8: every done entry
        - Recent.m3u8: last N entries by processed date

    Args:
        config: MediaSync configuration.
        notion: Notion client instance.
        storage: Storage backend instance.

    Returns:
        Number of playlists uploaded.
    """
    count = 0
    for profile in config.profiles:
        try:
            uploaded = _generate_for_profile(
                profile.name, config, notion, storage
            )
            count += uploaded
        except Exception as exc:
            logger.error(
                "Failed to generate playlists for profile %s: %s",
                profile.name, exc,
            )
    return count


def _generate_for_profile(
    profile_name: str,
    config: Config,
    notion: NotionClient,
    storage: StorageBackend,
) -> int:
    """Generate All and Recent playlists for a single profile."""
    done_entries = notion.get_done_for_profile(profile_name)
    if not done_entries:
        logger.info("No done entries for profile %s, skipping playlists", profile_name)
        return 0

    # Filter entries that have file keys (actually uploaded)
    entries_with_files = [e for e in done_entries if e.file_key]
    if not entries_with_files:
        return 0

    playlist_folder = f"{config.prefix}/{profile_name}/playlists"
    uploaded = 0

    # Generate "All" playlist
    all_items = _entries_to_playlist_items(entries_with_files, playlist_folder)
    if all_items:
        path = generate_m3u("All", all_items, config.output_dir)
        try:
            storage.upload(path, playlist_folder, path.name)
            uploaded += 1
            logger.info("Uploaded All.m3u8 for %s (%d items)", profile_name, len(all_items))
        finally:
            path.unlink(missing_ok=True)

    # Generate "Recent" playlist (last N by file key order, which is insertion order)
    recent_entries = entries_with_files[-DEFAULT_RECENT_COUNT:]
    recent_items = _entries_to_playlist_items(recent_entries, playlist_folder)
    if recent_items:
        path = generate_m3u("Recent", recent_items, config.output_dir)
        try:
            storage.upload(path, playlist_folder, path.name)
            uploaded += 1
            logger.info("Uploaded Recent.m3u8 for %s (%d items)", profile_name, len(recent_items))
        finally:
            path.unlink(missing_ok=True)

    return uploaded


def _entries_to_playlist_items(entries: list, playlist_folder: str) -> list[dict]:
    """Convert Notion entries to playlist item dicts with relative paths."""
    items = []
    for entry in entries:
        # Each entry may have multiple file keys (audio + video from "both")
        keys = [k.strip() for k in entry.file_key.split(";") if k.strip()]
        # Use relative paths from the playlist folder
        relative_keys = make_relative_keys(playlist_folder, keys)

        for rel_key in relative_keys:
            # Extract title from filename (remove extension)
            filename = rel_key.rsplit("/", 1)[-1] if "/" in rel_key else rel_key
            title = filename.rsplit(".", 1)[0] if "." in filename else filename

            items.append({
                "remote_key": rel_key,
                "title": title,
                "artist": entry.profile,
                "duration_secs": -1,  # Not stored per-file in Notion
            })
    return items