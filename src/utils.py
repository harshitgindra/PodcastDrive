"""Utility functions for YouTube Playlist to Podcast."""

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse


def extract_playlist_id(url: str) -> str:
    """Extract a playlist or channel ID from a YouTube URL.

    Supports:
    - Playlist URLs: ``https://www.youtube.com/playlist?list=PLxyz``
    - Channel URLs: ``https://www.youtube.com/@Handle/videos``
    - Channel URLs: ``https://www.youtube.com/channel/UCxyz``
    - Raw IDs: ``PLxyz``, ``UCxyz``, ``UUxyz``

    For channel URLs, yt_dlp returns the channel ID (``UCxyz``) which is
    used directly as the S3 prefix.

    Args:
        url: YouTube playlist URL, channel URL, or raw ID.

    Returns:
        The playlist or channel ID string.

    Raises:
        ValueError: If no ID can be extracted.
    """
    # Already a raw ID (no URL scheme)
    if not url.startswith("http"):
        return url

    parsed = urlparse(url)

    # Playlist URL: ?list=PLxyz
    params = parse_qs(parsed.query)
    playlist_id = params.get("list", [None])[0]
    if playlist_id:
        return playlist_id

    # Channel URL: /channel/UCxyz
    match = re.search(r"/channel/(UC[a-zA-Z0-9_-]+)", parsed.path)
    if match:
        return match.group(1)

    # Handle URL: /@Handle or /@Handle/videos
    match = re.search(r"/@([a-zA-Z0-9_.-]+)", parsed.path)
    if match:
        # Use the handle as the ID — yt_dlp will resolve it
        return f"@{match.group(1)}"

    raise ValueError(f"Could not extract playlist or channel ID from URL: {url}")


def parse_upload_date(date_str: str) -> datetime:
    """Parse a YYYYMMDD date string into a timezone-aware datetime.

    Args:
        date_str: Date string in YYYYMMDD format (e.g. ``"20250101"``).

    Returns:
        A :class:`datetime` with UTC timezone. Falls back to today's UTC date
        if *date_str* is not a valid YYYYMMDD string.
    """
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
