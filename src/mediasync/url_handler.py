"""URL handling for MediaSync — normalize and detect source platform.

Supports YouTube, Spotify, Apple Music, and generic URLs that yt-dlp
can handle. For non-YouTube URLs, uses yt-dlp'\''s built-in search/extractors.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def is_youtube_url(url: str) -> bool:
    """Check if URL is a YouTube video or playlist."""
    parsed = urlparse(url)
    return parsed.hostname in (
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "youtu.be", "music.youtube.com",
    )


def is_spotify_url(url: str) -> bool:
    """Check if URL is a Spotify track, album, or playlist."""
    parsed = urlparse(url)
    return parsed.hostname in ("open.spotify.com", "spotify.com")


def is_apple_music_url(url: str) -> bool:
    """Check if URL is an Apple Music link."""
    parsed = urlparse(url)
    return parsed.hostname in ("music.apple.com",)


def get_source_platform(url: str) -> str:
    """Identify the source platform for a URL.

    Returns:
        Platform identifier: "youtube", "spotify", "apple_music", or "other".
    """
    if is_youtube_url(url):
        return "youtube"
    if is_spotify_url(url):
        return "spotify"
    if is_apple_music_url(url):
        return "apple_music"
    return "other"


def normalize_url(url: str) -> str:
    """Normalize a URL for yt-dlp processing.

    YouTube URLs are passed through as-is.
    Spotify/Apple Music URLs are also passed through since yt-dlp has
    built-in extractors for them (requires spotdl or cookies for Spotify).
    Plain text queries can be prefixed with "ytsearch:" for YouTube search.

    Args:
        url: Input URL or search query.

    Returns:
        Normalized URL suitable for yt-dlp.
    """
    url = url.strip()

    # Already a URL
    if url.startswith("http://") or url.startswith("https://"):
        return url

    # Treat as a YouTube search query
    logger.info("Treating input as YouTube search: %s", url)
    return f"ytsearch:{url}"


def is_supported_url(url: str) -> bool:
    """Check if a URL is from a supported platform.

    All HTTP(S) URLs are considered supported since yt-dlp has a wide
    range of extractors. This is a soft check to catch obviously invalid input.
    """
    url = url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        return True
    # Allow search-style queries (will be prefixed with ytsearch:)
    if len(url) > 3 and not url.startswith("/"):
        return True
    return False