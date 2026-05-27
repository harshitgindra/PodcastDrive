"""RSS podcast episode fetcher and MP3 downloader.

Handles:
  - Resolving Apple Podcasts / iTunes URLs to the real RSS feed URL
    (via the iTunes Search API) and writing it back to Notion.
  - Parsing RSS feeds to find recent episodes.
  - Downloading episode MP3 files over HTTP.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import certifi
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class EpisodeMeta:
    """Metadata for a single podcast episode."""

    title: str
    url: str           # Direct MP3/audio URL
    pub_date: datetime
    guid: str
    duration: int = 0  # seconds, 0 if unknown
    thumbnail: str = ""  # Episode artwork URL (from itunes:image)


# ---------------------------------------------------------------------------
# iTunes / Apple Podcasts URL resolution
# ---------------------------------------------------------------------------

_APPLE_PODCASTS_RE = re.compile(
    r"(?:podcasts\.apple\.com|itunes\.apple\.com).*?/id(\d+)",
    re.IGNORECASE,
)


def is_apple_podcasts_url(url: str) -> bool:
    """Return True if *url* is an Apple Podcasts or iTunes URL."""
    return bool(_APPLE_PODCASTS_RE.search(url))


def resolve_feed_url(url: str) -> str:
    """Resolve an Apple Podcasts URL to its underlying RSS feed URL.

    If *url* is not an Apple Podcasts link it is returned as-is.  If
    resolution fails the original *url* is returned so the caller can
    fall back gracefully.

    Uses the iTunes Search API:
    ``https://itunes.apple.com/lookup?id=<podcast_id>&entity=podcast``

    Args:
        url: An Apple Podcasts URL or a direct RSS feed URL.

    Returns:
        The RSS feed URL, or *url* unchanged if resolution is not needed
        or fails.
    """
    match = _APPLE_PODCASTS_RE.search(url)
    if not match:
        return url  # Already a direct RSS URL

    podcast_id = match.group(1)
    lookup_url = f"https://itunes.apple.com/lookup?id={podcast_id}&entity=podcast"

    logger.info("[PodcastDownloader] Resolving Apple Podcasts id=%s via iTunes API", podcast_id)
    try:
        req = urllib.request.Request(
            lookup_url,
            headers={"User-Agent": "PodcastDrive/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = data.get("results", [])
        if not results:
            logger.warning("[PodcastDownloader] iTunes lookup returned no results for id=%s", podcast_id)
            return url

        feed_url = results[0].get("feedUrl", "")
        if not feed_url:
            logger.warning("[PodcastDownloader] iTunes result has no feedUrl for id=%s", podcast_id)
            return url

        logger.info("[PodcastDownloader] Resolved feed URL: %s", feed_url)
        return feed_url

    except Exception as exc:
        logger.warning("[PodcastDownloader] iTunes API lookup failed for id=%s: %s", podcast_id, exc)
        return url


def search_feed_url_by_name(name: str) -> str:
    """Search the iTunes Search API for a podcast by name and return its feed URL.

    Uses ``https://itunes.apple.com/search?term=<name>&entity=podcast&limit=1``.

    Args:
        name: Human-readable podcast name (e.g. ``"9to5Mac Daily"``).

    Returns:
        The RSS feed URL of the best match, or an empty string if not found
        or the search fails.
    """
    if not name:
        return ""

    term = urllib.parse.quote(name)
    search_url = f"https://itunes.apple.com/search?term={term}&entity=podcast&limit=1"

    logger.info("[PodcastDownloader] Searching iTunes for podcast: %r", name)
    try:
        req = urllib.request.Request(
            search_url,
            headers={"User-Agent": "PodcastDrive/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = data.get("results", [])
        if not results:
            logger.warning("[PodcastDownloader] iTunes search returned no results for: %r", name)
            return ""

        feed_url = results[0].get("feedUrl", "")
        if not feed_url:
            logger.warning("[PodcastDownloader] iTunes search result has no feedUrl for: %r", name)
            return ""

        track_name = results[0].get("trackName") or results[0].get("collectionName", "")
        logger.info(
            "[PodcastDownloader] Found feed URL for %r → matched %r: %s",
            name, track_name, feed_url,
        )
        return feed_url

    except Exception as exc:
        logger.warning("[PodcastDownloader] iTunes search failed for %r: %s", name, exc)
        return ""


# ---------------------------------------------------------------------------
# RSS feed parsing
# ---------------------------------------------------------------------------

# Namespace map used by most podcast feeds
_NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def _parse_duration(raw: str) -> int:
    """Parse an iTunes duration string (HH:MM:SS, MM:SS, or seconds) to int."""
    if not raw:
        return 0
    parts = raw.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return 0


_fetch_feed_attempt = retry(
    retry=retry_if_exception_type((OSError, urllib.error.URLError, TimeoutError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)


def fetch_feed_xml(feed_url: str) -> bytes:
    """Download the RSS feed at *feed_url* and return raw bytes.

    Retries up to 3 times on transient network errors.

    Args:
        feed_url: The URL of the RSS/Atom feed.

    Returns:
        Raw feed bytes.

    Raises:
        RuntimeError: If the HTTP request fails after all retries.
    """

    @_fetch_feed_attempt
    def _attempt() -> bytes:
        req = urllib.request.Request(
            feed_url,
            headers={"User-Agent": "PodcastDrive/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            return resp.read()

    logger.info("[PodcastDownloader] Fetching RSS feed: %s", feed_url)
    try:
        return _attempt()
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Failed to fetch RSS feed {feed_url}: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch RSS feed {feed_url}: {exc}") from exc


def parse_channel_thumbnail(feed_xml: bytes) -> str:
    """Extract the channel-level artwork URL from an RSS feed.

    Checks (in order of preference):
    1. ``<itunes:image href="...">`` inside ``<channel>``
    2. ``<image><url>...</url></image>`` inside ``<channel>``

    Args:
        feed_xml: Raw bytes of the RSS feed.

    Returns:
        Artwork URL string, or ``""`` if none found.
    """
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError:
        return ""

    channel = root.find("channel")
    if channel is None:
        return ""

    # 1. <itunes:image href="...">
    itunes_img = channel.find("itunes:image", _NS)
    if itunes_img is not None:
        href = itunes_img.get("href", "")
        if href:
            return href

    # 2. Standard RSS <image><url>...</url></image>
    rss_image = channel.find("image")
    if rss_image is not None:
        url_el = rss_image.find("url")
        if url_el is not None and url_el.text:
            return url_el.text.strip()

    return ""


def parse_episodes(feed_xml: bytes, max_age_days: int | None = None) -> list[EpisodeMeta]:
    """Parse RSS *feed_xml* bytes and return a list of :class:`EpisodeMeta`.

    Args:
        feed_xml:     Raw bytes of the RSS feed.
        max_age_days: If set, episodes older than this many days are excluded.

    Returns:
        List of :class:`EpisodeMeta` in feed order (newest first, typically).
    """
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError as exc:
        raise RuntimeError(f"Failed to parse RSS XML: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        return []

    cutoff: datetime | None = None
    if max_age_days is not None:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    # Channel-level thumbnail used as fallback for episodes without their own art
    channel_thumbnail = parse_channel_thumbnail(feed_xml)

    episodes: list[EpisodeMeta] = []

    for item in channel.findall("item"):
        # Audio enclosure URL
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        audio_url = enclosure.attrib.get("url", "")
        if not audio_url:
            continue

        # Title
        title_el = item.find("title")
        title = title_el.text.strip() if title_el is not None and title_el.text else "Untitled"

        # GUID (fall back to audio URL)
        guid_el = item.find("guid")
        guid = (guid_el.text or audio_url).strip() if guid_el is not None else audio_url

        # Publication date
        pub_date_el = item.find("pubDate")
        pub_date: datetime
        try:
            pub_date = parsedate_to_datetime(pub_date_el.text or "")
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
        except Exception:
            pub_date = datetime.now(timezone.utc)

        # Age filter
        if cutoff and pub_date < cutoff:
            logger.debug("[PodcastDownloader] Skipping old episode '%s' (pub_date=%s)", title, pub_date.date())
            continue

        # Duration (itunes:duration)
        duration_el = item.find("itunes:duration", _NS)
        duration = _parse_duration(duration_el.text if duration_el is not None else "")

        # Episode artwork: prefer per-item <itunes:image href="...">,
        # fall back to channel-level thumbnail so every episode has art.
        thumbnail = ""
        item_img = item.find("itunes:image", _NS)
        if item_img is not None:
            thumbnail = item_img.get("href", "")
        if not thumbnail:
            thumbnail = channel_thumbnail

        episodes.append(EpisodeMeta(
            title=title,
            url=audio_url,
            pub_date=pub_date,
            guid=guid,
            duration=duration,
            thumbnail=thumbnail,
        ))

    logger.info("[PodcastDownloader] Parsed %d episodes from feed", len(episodes))
    return episodes


# ---------------------------------------------------------------------------
# Episode ID (stable key for S3 / de-duplication)
# ---------------------------------------------------------------------------

def episode_id_from_guid(guid: str, podcast_slug: str) -> str:
    """Derive a filesystem/S3-safe episode ID from a feed GUID.

    Takes the last path-segment of the GUID URL (or the whole string if it is
    not a URL), strips query strings, and limits to 80 characters.

    Args:
        guid:         The ``<guid>`` value from the RSS item.
        podcast_slug: Short identifier for the podcast (used as prefix).

    Returns:
        A slug like ``"my-podcast--abc123def456"``.
    """
    # Strip query string / fragment
    clean = guid.split("?")[0].split("#")[0]
    # Take the last non-empty path segment
    segment = [s for s in clean.rstrip("/").split("/") if s]
    base = segment[-1] if segment else clean
    # Keep only safe characters
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:80]
    return safe


# ---------------------------------------------------------------------------
# HTTP download
# ---------------------------------------------------------------------------

_download_retry = retry(
    retry=retry_if_exception_type((OSError, urllib.error.URLError, TimeoutError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=5, max=60),
    reraise=True,
)


def download_episode(url: str, episode_id: str, tmp_dir: str) -> str:
    """Download a podcast episode MP3 from *url* to *tmp_dir*.

    Supports HTTP range-request resumption: if a partial file already exists
    from a previous interrupted download, a ``Range: bytes=N-`` header is sent
    so only the missing bytes are transferred.  Falls back to a full download
    when the server does not support range requests (returns 200 instead of 206).

    Retries up to 3 times on transient network errors.

    Args:
        url:        Direct MP3 audio URL.
        episode_id: Used to name the local file.
        tmp_dir:    Directory to write the file into.

    Returns:
        Local path to the downloaded file.

    Raises:
        RuntimeError: If the download fails after all retries.
    """
    local_path = os.path.join(tmp_dir, f"{episode_id}.mp3")
    logger.info("[PodcastDownloader] Downloading episode %s from %s", episode_id, url)

    @_download_retry
    def _attempt() -> None:
        existing_bytes = os.path.getsize(local_path) if os.path.exists(local_path) else 0

        headers: dict[str, str] = {"User-Agent": "PodcastDrive/1.0"}
        if existing_bytes > 0:
            headers["Range"] = f"bytes={existing_bytes}-"
            logger.info(
                "[PodcastDownloader] Resuming %s from byte %d",
                episode_id, existing_bytes,
            )

        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=300, context=_SSL_CTX)
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                # Range not satisfiable — file already complete
                logger.info("[PodcastDownloader] %s already fully downloaded (416)", episode_id)
                return
            raise

        with resp:
            status = getattr(resp, "status", resp.getcode())
            if status == 206:
                # Partial content — append to existing file
                open_mode = "ab"
            else:
                # Full response (server ignored Range header) — start fresh
                if existing_bytes:
                    logger.info(
                        "[PodcastDownloader] Server returned %d (no range support) — restarting %s",
                        status, episode_id,
                    )
                open_mode = "wb"

            with open(local_path, open_mode) as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)

    try:
        _attempt()
    except Exception as exc:
        if os.path.exists(local_path):
            os.remove(local_path)
        raise RuntimeError(f"Failed to download episode {episode_id}: {exc}") from exc

    size_mb = os.path.getsize(local_path) / (1024 * 1024)
    logger.info("[PodcastDownloader] Downloaded %s (%.1f MiB)", episode_id, size_mb)
    return local_path
