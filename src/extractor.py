"""Playlist extraction using yt_dlp for YouTube metadata."""

import logging

import yt_dlp

from models import PlaylistMeta, VideoEntry
from utils import extract_playlist_id
from ytdlp_cookies import inject_cookies
from ytdlp_runtime import inject_remote_components

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
    inject_remote_components(ydl_opts)

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


class ExtractionError(Exception):
    """Raised when metadata extraction fails for a reason that may be transient.

    Distinct from a *genuinely unavailable* video (deleted, private,
    region-blocked, members-only), which is reported by returning ``None``.
    Infrastructure faults — a broken JS challenge solver, network errors, HTTP
    5xx, rate limiting — surface as this exception so the caller counts them as
    failures instead of silently writing the episode off forever.
    """


#: Substrings that identify a video as permanently unavailable to us.  Anything
#: not matching one of these is treated as a retryable extraction failure.
_PERMANENT_UNAVAILABILITY_MARKERS = (
    "video unavailable",
    "private video",
    "this video is private",
    "has been removed",
    "removed by the uploader",
    "removed for violating",
    "account associated with this video has been terminated",
    "who has blocked it in your country",
    "not available in your country",
    "not made this video available in your country",
    "video is not available",
    "members-only",
    "join this channel",
    "available to this channel's members",
    "is not available on this app",
    "this live event has ended",
    "requested video is unavailable",
    "age-restricted",
    "inappropriate for some users",
)


def _is_permanently_unavailable(message: str) -> bool:
    """Return ``True`` when *message* names a permanent unavailability reason.

    Args:
        message: The yt-dlp error text (any case).
    """
    lowered = message.lower()
    return any(marker in lowered for marker in _PERMANENT_UNAVAILABILITY_MARKERS)


def extract_video_metadata(video_url: str) -> dict | None:
    """Extract full metadata for a single video.

    Args:
        video_url: YouTube video URL.

    Returns:
        Dict with upload_date, description, thumbnail, duration, title.
        None if the video is genuinely unavailable (deleted, private,
        region-blocked, members-only).

    Raises:
        BotDetectedError: YouTube is blocking requests (cookies expired or IP flagged).
        ExtractionError: Extraction failed for a potentially transient reason
            (broken JS challenge solver, network fault, rate limiting).  The
            episode should be retried on a later run.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": False,
    }
    inject_cookies(ydl_opts)
    inject_remote_components(ydl_opts)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        if not info:
            raise ExtractionError(f"yt-dlp returned no metadata for {video_url}")

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
        msg = str(exc)
        lowered = msg.lower()
        if "sign in to confirm" in lowered or "bot" in lowered:
            raise BotDetectedError(
                f"YouTube bot detection triggered for {video_url}. "
                "Cookies are expired or missing authentication. "
                "Refresh with: ./refresh_cookies.sh"
            ) from exc

        if _is_permanently_unavailable(msg):
            logger.info("Video unavailable %s: %s", video_url, exc)
            return None

        # Anything else is an extraction fault, not a missing video.  Most
        # commonly "Requested format is not available", which means yt-dlp could
        # not solve the n challenge and returned no media formats at all.
        raise ExtractionError(f"Extraction failed for {video_url}: {msg}") from exc
    except BotDetectedError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Extraction failed for {video_url}: {exc}") from exc
