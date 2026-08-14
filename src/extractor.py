"""Playlist extraction using yt_dlp for YouTube metadata."""

import logging

import yt_dlp

from models import PlaylistMeta, VideoEntry
from utils import extract_playlist_id
from ytdlp_cookies import inject_cookies

logger = logging.getLogger(__name__)


def extract_playlist(playlist_url: str) -> tuple[PlaylistMeta, list[VideoEntry]]:
    """Extract basic metadata for all videos in a YouTube playlist.

    Uses flat extraction to quickly list videos without visiting each one.
    Fields like ``upload_date`` and ``description`` will be empty — use
    :func:`extract_video_metadata` to fill them in for specific videos.

    Args:
        playlist_url: Full YouTube playlist URL.

    Returns:
        Tuple of (playlist metadata, list of video entries).
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
    }
    inject_cookies(ydl_opts)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(playlist_url, download=False)

    playlist_id = extract_playlist_id(playlist_url)

    title = (result.get("title") or "").strip()
    if not title:
        title = "YouTube Playlist Podcast"

    # Extract the best available playlist-level thumbnail
    playlist_thumbnail = ""
    thumbs = result.get("thumbnails") or []
    if thumbs:
        # yt-dlp lists thumbnails smallest-first; prefer the last (largest)
        playlist_thumbnail = thumbs[-1].get("url", "")
    if not playlist_thumbnail:
        playlist_thumbnail = result.get("thumbnail") or ""

    playlist_meta = PlaylistMeta(
        title=title,
        description=result.get("description") or "",
        uploader=result.get("uploader") or result.get("channel") or "",
        channel_url=result.get("channel_url") or "",
        webpage_url=result.get("webpage_url") or "",
        playlist_id=playlist_id,
        thumbnail=playlist_thumbnail,
    )

    entries = result.get("entries") or []
    video_entries: list[VideoEntry] = []

    for idx, entry in enumerate(entries):
        if entry is None:
            continue

        video_id = (entry.get("id") or "").strip()
        if not video_id:
            continue

        thumbnail = ""
        thumbs = entry.get("thumbnails") or []
        if thumbs:
            thumbnail = thumbs[-1].get("url", "")

        video_entries.append(
            VideoEntry(
                video_id=video_id,
                title=entry.get("title") or "",
                description="",
                duration=entry.get("duration"),
                upload_date="",
                thumbnail=thumbnail,
                webpage_url=f"https://www.youtube.com/watch?v={video_id}",
                playlist_index=entry.get("playlist_index") or (idx + 1),
                live_status=entry.get("live_status"),
            )
        )

    logger.info(
        "Extracted %d videos from playlist '%s' (%s)",
        len(video_entries),
        playlist_meta.title,
        playlist_id,
    )

    return playlist_meta, video_entries


class BotDetectedError(Exception):
    """Raised when YouTube returns a bot-detection challenge."""

    pass


def extract_video_metadata(video_url: str) -> dict | None:
    """Extract full metadata for a single video.

    Args:
        video_url: YouTube video URL.

    Returns:
        Dict with upload_date, description, thumbnail, duration, title.
        None if video is genuinely unavailable (deleted/private/region-blocked).

    Raises:
        BotDetectedError: YouTube is blocking requests (cookies expired or IP flagged).
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": False,
    }
    inject_cookies(ydl_opts)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        if not info:
            return None

        return {
            "upload_date": info.get("upload_date") or "",
            "description": info.get("description") or "",
            "thumbnail": info.get("thumbnail") or "",
            "duration": info.get("duration"),
            "title": info.get("title") or "",
            "live_status": info.get("live_status"),
            "chapters": info.get("chapters") or [],
        }
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc).lower()
        if "sign in to confirm" in msg or "bot" in msg:
            raise BotDetectedError(
                f"YouTube bot detection triggered for {video_url}. "
                "Cookies are expired or missing authentication. "
                "Refresh with: ./refresh_cookies.sh"
            ) from exc
        # Genuine unavailability (private, deleted, region-blocked)
        logger.info("Video unavailable %s: %s", video_url, exc)
        return None
    except Exception as exc:
        logger.warning("Failed to extract metadata for %s: %s", video_url, exc)
        return None
