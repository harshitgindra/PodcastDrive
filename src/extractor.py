"""Playlist extraction using yt_dlp for YouTube metadata."""

import logging
import re

import yt_dlp

from models import PlaylistMeta, VideoEntry
from utils import extract_playlist_id
from ytdlp_cookies import inject_cookies
from ytdlp_runtime import inject_remote_components

logger = logging.getLogger(__name__)

#: Channel-tab path suffixes.  A bare channel root (``/@handle``,
#: ``/channel/UC...``, ``/c/name``, ``/user/name``) flat-extracts into these
#: *tabs* as pseudo-entries (``Videos``/``Live``/``Shorts``), each with no
#: ``duration`` — which the sync pipeline then skips as if they were live
#: streams.  Resolving the root to its ``/videos`` tab yields the real catalog.
_CHANNEL_TAB_SUFFIXES = (
    "videos",
    "streams",
    "shorts",
    "playlists",
    "featured",
    "community",
    "live",
    "podcasts",
    "releases",
    "courses",
    "store",
    "search",
)

#: A bare channel-root path: ``/@handle``, ``/channel/UC...``, ``/c/name`` or
#: ``/user/name`` with no trailing tab segment.
_CHANNEL_ROOT_RE = re.compile(
    r"^/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/?$"
)


def _normalize_channel_url(url: str) -> str:
    """Append ``/videos`` to a bare YouTube channel-root URL.

    A channel root flat-extracts into tab pseudo-entries rather than videos, so
    the pipeline sees ``duration=None`` entries and skips them all.  Explicit
    playlist URLs (``?list=``), already-tabbed channel URLs, and non-YouTube
    URLs are returned unchanged.

    Args:
        url: The configured playlist/channel URL.

    Returns:
        The URL, with ``/videos`` appended when it was a bare channel root.
    """
    from urllib.parse import urlparse, urlunparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return url

    host = (parsed.netloc or "").lower()
    if "youtube.com" not in host:
        return url
    if "list=" in (parsed.query or ""):
        return url
    if not _CHANNEL_ROOT_RE.match(parsed.path or ""):
        return url

    new_path = parsed.path.rstrip("/") + "/videos"
    normalized = urlunparse(parsed._replace(path=new_path))
    logger.info(
        "Normalized channel root %s -> %s (bare roots list tabs, not videos)",
        url,
        normalized,
    )
    return normalized


def _looks_like_channel_tabs(entries: list) -> bool:
    """Return ``True`` when flat entries are channel tabs, not videos.

    yt-dlp represents a channel root's tabs as ``url``-type entries whose URL
    path ends in a tab suffix (``/videos``, ``/shorts``, ...).  Real video
    entries carry an 11-char watch id and never a tab suffix.
    """
    from urllib.parse import urlparse

    tab_like = 0
    seen = 0
    for entry in entries:
        if not entry:
            continue
        seen += 1
        url = _entry_url(entry)
        path = (urlparse(url).path or "").rstrip("/").lower()
        suffix = path.rsplit("/", 1)[-1] if path else ""
        if suffix in _CHANNEL_TAB_SUFFIXES:
            tab_like += 1
    # Every real entry is a tab -> this was a channel root, not a video list.
    return seen > 0 and tab_like == seen



def _entry_url(entry: dict) -> str:
    """The best URL for a flat entry.

    Channel-tab pseudo-entries carry their tab URL in ``webpage_url`` (``url``
    is ``None``); ordinary video entries use ``url``.  Prefer whichever is set.
    """
    return entry.get("webpage_url") or entry.get("url") or ""


def _videos_tab_from_entries(entries: list) -> str:
    """Return the ``/videos`` tab URL from channel-tab entries, or ``""``.

    Given the tab pseudo-entries of a channel root, prefer the Videos tab;
    fall back to the first entry's channel-root URL with ``/videos`` appended.
    """
    from urllib.parse import urlparse, urlunparse

    for entry in entries:
        if not entry:
            continue
        url = _entry_url(entry)
        path = (urlparse(url).path or "").rstrip("/").lower()
        if path.rsplit("/", 1)[-1] == "videos":
            return url
    # No explicit Videos tab entry: derive it from any tab URL's channel root.
    for entry in entries:
        if not entry:
            continue
        url = _entry_url(entry)
        parsed = urlparse(url)
        path = (parsed.path or "").rstrip("/")
        if not path:
            continue
        root = path.rsplit("/", 1)[0] or path
        return urlunparse(parsed._replace(path=root + "/videos"))
    return ""


def _flat_extract(playlist_url: str) -> dict:
    """Flat-extract *playlist_url* with yt-dlp and return the result dict.

    Raises:
        BotDetectedError: YouTube returned a bot-detection challenge.
        ExtractionError: yt-dlp failed or returned no data (treat as retryable).
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
    }
    inject_cookies(ydl_opts)
    inject_remote_components(ydl_opts)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(playlist_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        # ignoreerrors only suppresses *per-entry* failures; a playlist-level
        # fault (network, HTTP 5xx, bot challenge) still raises here.
        if _is_bot_detection(str(exc)):
            raise BotDetectedError(
                f"YouTube bot detection triggered for playlist {playlist_url}. "
                "Cookies are expired or missing authentication. "
                "Refresh with: ./refresh_cookies.sh"
            ) from exc
        raise ExtractionError(f"Playlist extraction failed for {playlist_url}: {exc}") from exc

    if not result:
        # extract_info returns None when yt-dlp gave up entirely.  Previously
        # result.get(...) raised AttributeError here, which callers logged as an
        # opaque crash instead of a retryable extraction fault.
        raise ExtractionError(
            f"yt-dlp returned no playlist data for {playlist_url} "
            "(throttling, an unsolved JS challenge, or a deleted playlist)"
        )
    return result


def extract_playlist(playlist_url: str) -> tuple[PlaylistMeta, list[VideoEntry]]:
    """Extract basic metadata for all videos in a YouTube playlist.

    Uses flat extraction to quickly list videos without visiting each one.
    Fields like ``upload_date`` and ``description`` will be empty — use
    :func:`extract_video_metadata` to fill them in for specific videos.

    Args:
        playlist_url: Full YouTube playlist URL.

    Returns:
        Tuple of (playlist metadata, list of video entries).

    Raises:
        BotDetectedError: YouTube returned a bot-detection challenge.
        ExtractionError: yt-dlp returned no playlist data, or failed for a
            potentially transient reason.  Callers must not treat this as an
            empty playlist (see sync._orphan_deletion_is_safe).
    """
    # A bare channel root lists tabs, not videos; resolve it to /videos first.
    playlist_url = _normalize_channel_url(playlist_url)

    result = _flat_extract(playlist_url)

    # Safety net: if yt-dlp still handed back channel tabs (e.g. the URL was a
    # channel root in a form the normalizer did not match), re-extract the
    # Videos tab once so the pipeline sees real episodes instead of skipping
    # every duration-less tab as a live stream.
    entries_raw = result.get("entries") or []
    if _looks_like_channel_tabs(entries_raw):
        videos_tab = _videos_tab_from_entries(entries_raw)
        if videos_tab and videos_tab != playlist_url:
            logger.info(
                "Playlist %s returned channel tabs; re-extracting Videos tab %s",
                playlist_url,
                videos_tab,
            )
            playlist_url = videos_tab
            result = _flat_extract(playlist_url)

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


#: Phrases that specifically identify YouTube's bot-detection challenge.  These
#: must stay narrow: a bare "bot" substring also matches "robot", "bottom",
#: "sabotage", and any bot-containing video title echoed back in the error text,
#: and a false positive aborts the entire playlist run (see sync.process_playlist).
_BOT_DETECTION_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm you are not a bot",
    "not a bot",
    "bot detect",
    "suspected bot",
    "unusual traffic",
)


def _is_bot_detection(message: str) -> bool:
    """Return ``True`` when *message* is YouTube's bot-detection challenge.

    Args:
        message: The yt-dlp error text (any case).
    """
    lowered = message.lower()
    return any(marker in lowered for marker in _BOT_DETECTION_MARKERS)


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
        if _is_bot_detection(msg):
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
