"""Playlist sync — ensures all items from YouTube playlists are in the target.

On each run, scans "done" entries with playlist URLs, fetches current playlist
contents from YouTube, and creates new "pending" Notion entries for any videos
not already present (downloaded or queued). Removals from the YouTube playlist
are ignored — already-downloaded content stays.
"""

from __future__ import annotations

import logging
import re

from mediasync.downloader import DownloadError, get_playlist_metadata, is_playlist
from mediasync.notion_client import MediaEntry, NotionClient

logger = logging.getLogger(__name__)


def sync_playlists(notion: NotionClient, profiles: list[str]) -> int:
    """Sync YouTube playlists for all profiles.

    For each "done" playlist entry, checks if all videos in the playlist
    are represented in Notion (any status). Creates "pending" entries for
    missing videos.

    Returns:
        Number of new entries created.
    """
    created = 0

    for profile in profiles:
        all_entries = notion.get_all_for_profile(profile)
        # Build set of known video URLs/IDs for this profile
        known_urls = _build_known_set(all_entries)

        # Find done entries that are playlists
        playlist_entries = [
            e for e in all_entries
            if e.status.value == "done" and is_playlist(e.url)
        ]

        for entry in playlist_entries:
            new = _sync_single_playlist(entry, notion, known_urls)
            created += new

    return created


def _sync_single_playlist(
    entry: MediaEntry,
    notion: NotionClient,
    known_urls: set[str],
) -> int:
    """Sync a single playlist entry. Returns number of new entries created."""
    try:
        items = get_playlist_metadata(entry.url)
    except DownloadError as exc:
        logger.warning("Could not fetch playlist %s: %s", entry.url, exc)
        return 0

    created = 0
    for item in items:
        video_url = item.get("url") or item.get("webpage_url", "")
        if not video_url:
            video_id = item.get("id", "")
            if not video_id:
                continue
            video_url = f"https://www.youtube.com/watch?v={video_id}"

        video_id = _extract_video_id(video_url)
        if video_id in known_urls:
            continue

        # Create new pending entry with same profile and format
        page_id = notion.create_entry(video_url, entry.profile, entry.format)
        if page_id:
            logger.info("Playlist sync: added %s for profile %s", video_url, entry.profile)
            known_urls.add(video_id)  # Prevent duplicates within same run
            created += 1

    if created:
        logger.info(
            "Playlist %s: added %d new entries for profile %s",
            entry.url, created, entry.profile,
        )
    return created


def _build_known_set(entries: list[MediaEntry]) -> set[str]:
    """Build a set of known video IDs from existing entries."""
    known: set[str] = set()
    for e in entries:
        vid = _extract_video_id(e.url)
        if vid:
            known.add(vid)
    return known


def _extract_video_id(url: str) -> str:
    """Extract YouTube video ID from URL. Falls back to full URL as key."""
    # Standard watch URL
    match = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", url)
    if match:
        return match.group(1)
    # Short URL
    match = re.search(r"youtu\.be/([a-zA-Z0-9_-]{11})", url)
    if match:
        return match.group(1)
    # Fallback: use full URL as identifier
    return url
