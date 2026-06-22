"""RSS 2.0 feed generator with iTunes namespace extensions.

Builds a podcast-compatible RSS feed from playlist metadata and episode
information, using CloudFront URLs for audio enclosures.
"""

import logging
import os
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import format_datetime
from xml.dom.minidom import parseString

from models import EpisodeMeta, PlaylistMeta, VideoEntry
from s3_manager import S3Manager
from utils import parse_upload_date

logger = logging.getLogger(__name__)

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

# Register the itunes namespace prefix so ElementTree uses it in output
ET.register_namespace("itunes", ITUNES_NS)


def _format_duration(seconds: int | None) -> str:
    """Format a duration in seconds as ``H:MM:SS`` or ``M:SS``.

    Args:
        seconds: Duration in seconds, or ``None``.

    Returns:
        Formatted duration string.  Returns ``"0:00"`` when *seconds* is
        ``None`` or non-positive.
    """
    if not seconds or seconds <= 0:
        return "0:00"

    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _first_paragraph(text: str) -> str:
    """Return the first paragraph of *text* (split on double newline).

    Returns the full text if there is no double-newline separator.
    """
    if not text:
        return ""
    parts = text.split("\n\n", 1)
    return parts[0].strip()


def _validate_cloudfront_base(cloudfront_base: str) -> None:
    """Validate that *cloudfront_base* looks like a usable HTTPS URL.

    Raises:
        ValueError: If the URL is empty, not HTTPS, or has a trailing slash.
    """
    if not cloudfront_base:
        raise ValueError(
            "cloudfront_base is empty — set the CLOUDFRONT_BASE environment variable"
        )
    if not cloudfront_base.startswith("https://"):
        raise ValueError(
            f"cloudfront_base must start with 'https://' (got: {cloudfront_base!r})"
        )
    if cloudfront_base.endswith("/"):
        raise ValueError(
            f"cloudfront_base must not have a trailing slash (got: {cloudfront_base!r})"
        )


def generate_rss(
    playlist_meta: PlaylistMeta,
    episodes: list[EpisodeMeta],
    cloudfront_base: str,
    playlist_id: str,
    language: str = "en",
) -> str:
    """Generate a podcast RSS 2.0 XML string with iTunes extensions.

    Args:
        playlist_meta: Playlist-level metadata.
        episodes: List of episode metadata (should already be sorted
            newest-first).
        cloudfront_base: CloudFront distribution base URL (no trailing
            slash).
        playlist_id: Playlist ID for URL construction.

    Returns:
        Pretty-printed RSS XML string.

    Raises:
        ValueError: If *cloudfront_base* is malformed.
    """
    _validate_cloudfront_base(cloudfront_base)

    rss = ET.Element("rss", version="2.0")
    # The xmlns:itunes attribute is added automatically by ET.register_namespace
    # when itunes-namespaced elements are serialized. No manual set needed.

    channel = ET.SubElement(rss, "channel")

    # --- Channel-level metadata ---
    _add_channel_metadata(channel, playlist_meta, episodes, cloudfront_base, playlist_id, language=language)

    # --- Item elements ---
    for episode in episodes:
        _add_item(channel, episode, cloudfront_base, playlist_id)

    # Pretty-print via minidom
    rough_string = ET.tostring(rss, encoding="unicode", xml_declaration=False)
    dom = parseString(rough_string)
    pretty = dom.toprettyxml(indent="  ", encoding=None)

    # Remove the XML declaration that minidom adds, then re-add a clean one
    lines = pretty.split("\n")
    if lines and lines[0].startswith("<?xml"):
        lines = lines[1:]
    xml_body = "\n".join(lines).strip()

    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}\n'


def _add_channel_metadata(
    channel: ET.Element,
    meta: PlaylistMeta,
    episodes: list[EpisodeMeta],
    cloudfront_base: str,
    playlist_id: str,
    language: str = "en",
) -> None:
    """Populate channel-level RSS and iTunes ``<channel>`` child elements.

    Sets standard RSS fields (title, link, description, language, lastBuildDate)
    and iTunes-namespace extensions (author, summary, explicit, owner, image).

    Args:
        channel: The ``<channel>`` :class:`xml.etree.ElementTree.Element` to
                 populate.
        meta: Playlist-level metadata.
        episodes: Episode list — used to pick a channel thumbnail from the
                  first episode (if available).
        cloudfront_base: CloudFront base URL (unused here, kept for symmetry).
        playlist_id: Playlist ID (unused here, kept for symmetry).
    """
    channel_link = meta.webpage_url or meta.channel_url
    suffix = os.environ.get("FEED_TITLE_SUFFIX", " ✂️")

    ET.SubElement(channel, "title").text = meta.title + suffix
    ET.SubElement(channel, "link").text = channel_link
    ET.SubElement(channel, "description").text = meta.description or meta.title
    ET.SubElement(channel, "language").text = language
    ET.SubElement(channel, "generator").text = "yt-podcast-lambda"

    now = datetime.now(UTC)
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)

    # Determine best artwork URL: prefer playlist-level thumbnail, fall back
    # to the first episode's thumbnail.
    artwork_url = meta.thumbnail or (episodes[0].thumbnail if episodes else "")

    # Standard RSS 2.0 <image> block (required by many non-iTunes podcast apps)
    if artwork_url:
        rss_image = ET.SubElement(channel, "image")
        ET.SubElement(rss_image, "url").text = artwork_url
        ET.SubElement(rss_image, "title").text = meta.title + suffix
        ET.SubElement(rss_image, "link").text = channel_link

    # iTunes tags
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = meta.uploader or meta.title
    ET.SubElement(channel, f"{{{ITUNES_NS}}}summary").text = meta.description or meta.title
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = "no"

    subtitle = os.environ.get("FEED_SUBTITLE", "Ad-free · PodcastDrive")
    if subtitle:
        ET.SubElement(channel, f"{{{ITUNES_NS}}}subtitle").text = subtitle

    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text = meta.uploader or meta.title

    # iTunes <itunes:image> — uses same resolved artwork URL
    if artwork_url:
        img = ET.SubElement(channel, f"{{{ITUNES_NS}}}image")
        img.set("href", artwork_url)


def _add_item(
    channel: ET.Element,
    episode: EpisodeMeta,
    cloudfront_base: str,
    playlist_id: str,
) -> None:
    """Append a single RSS ``<item>`` element to *channel*.

    Populates standard RSS fields (title, guid, enclosure, pubDate,
    description) and iTunes extensions (duration, summary, explicit,
    image, episode number).

    Args:
        channel: The parent ``<channel>`` element to append to.
        episode: Episode metadata including file size and CloudFront URL.
        cloudfront_base: CloudFront base URL (unused directly; URL already
                         pre-built in ``episode.cloudfront_url``).
        playlist_id: Playlist ID (unused directly; kept for API symmetry).
    """
    item = ET.SubElement(channel, "item")

    ep_title = episode.title
    if episode.ads_removed:
        ep_ad_suffix = os.environ.get("EPISODE_AD_REMOVED_SUFFIX", " ✂️")
        if ep_ad_suffix:
            ep_title += ep_ad_suffix
    ET.SubElement(item, "title").text = ep_title

    guid = ET.SubElement(item, "guid")
    guid.set("isPermaLink", "false")
    guid.text = episode.video_id

    enclosure = ET.SubElement(item, "enclosure")
    enclosure.set("url", episode.cloudfront_url)
    enclosure.set("length", str(episode.file_size))
    enclosure.set("type", "audio/mpeg")

    # pubDate in RFC 2822
    pub_dt = parse_upload_date(episode.upload_date)
    ET.SubElement(item, "pubDate").text = format_datetime(pub_dt)

    # Description: first paragraph of YouTube description + source link
    desc_text = _first_paragraph(episode.description)
    if episode.webpage_url:
        desc_text = f"{desc_text}\n\nSource: {episode.webpage_url}".strip()

    # Fix 8: Append chapter markers if available
    if hasattr(episode, "chapters") and episode.chapters:
        lines = ["\n\nChapters:"]
        for ch in episode.chapters:
            mins, secs = divmod(int(ch.get("start_time", 0)), 60)
            hrs, mins = divmod(mins, 60)
            ts = f"{hrs}:{mins:02d}:{secs:02d}" if hrs else f"{mins}:{secs:02d}"
            lines.append(f"{ts}  {ch.get('title', '')}")
        desc_text += "\n".join(lines)

    # Use AI-generated summary if available
    if hasattr(episode, "summary") and episode.summary:
        desc_text = episode.summary + "\n\n" + desc_text

    ET.SubElement(item, "description").text = desc_text

    # iTunes item tags
    ET.SubElement(item, f"{{{ITUNES_NS}}}duration").text = _format_duration(
        episode.duration
    )
    ET.SubElement(item, f"{{{ITUNES_NS}}}summary").text = desc_text
    ET.SubElement(item, f"{{{ITUNES_NS}}}explicit").text = "no"

    if episode.thumbnail:
        img = ET.SubElement(item, f"{{{ITUNES_NS}}}image")
        img.set("href", episode.thumbnail)

    if episode.playlist_index is not None:
        ET.SubElement(item, f"{{{ITUNES_NS}}}episode").text = str(
            episode.playlist_index
        )


def build_episode_metadata(
    video_entries: list[VideoEntry],
    final_keys: set[str],
    cloudfront_base: str,
    playlist_id: str,
    s3: S3Manager,
    ads_removed_ids: set[str] | None = None,
    manifest: dict | None = None,
) -> list[EpisodeMeta]:
    """Build a sorted list of :class:`EpisodeMeta` for episodes in S3.

    For each video_id in *final_keys* that has a matching entry in
    *video_entries*, creates an :class:`EpisodeMeta` with the S3 key,
    file size (from ``s3.get_object_size``), and CloudFront URL.

    Episodes are sorted by ``upload_date`` descending (newest first).

    Args:
        video_entries: All video entries from the playlist extraction.
        final_keys: Set of video_id strings currently in S3.
        cloudfront_base: CloudFront distribution base URL.
        playlist_id: Playlist ID for key/URL construction.
        s3: An :class:`S3Manager` instance for querying object sizes.

    Returns:
        List of :class:`EpisodeMeta` sorted newest-first.
    """
    entry_map = {e.video_id: e for e in video_entries}
    episodes: list[EpisodeMeta] = []
    seen_titles: set[str] = set()

    for video_id in final_keys:
        entry = entry_map.get(video_id)
        if not entry:
            # MP3 in S3 but not in current playlist extraction — skip
            logger.warning(
                "Video %s in S3 but not in playlist extraction; skipping",
                video_id,
            )
            continue

        # Deduplicate by normalised title to avoid showing the same episode
        # twice when a video is re-uploaded with a new ID.
        normalised_title = entry.title.strip().lower()
        if normalised_title in seen_titles:
            logger.warning(
                "Duplicate title '%s' (video_id=%s) — skipping to avoid duplicate episode in feed",
                entry.title,
                video_id,
            )
            continue
        seen_titles.add(normalised_title)

        # Use manifest upload_date if entry has empty date (flat extraction doesn't include it)
        upload_date = entry.upload_date
        if not upload_date and manifest:
            upload_date = manifest.get(video_id, {}).get("upload_date", "")
        if upload_date and upload_date != entry.upload_date:
            entry.upload_date = upload_date

        s3_key = f"{playlist_id}/episodes/{video_id}.mp3"
        try:
            file_size = s3.get_object_size(s3_key)
        except Exception:
            file_size = 0
            logger.warning("Could not get size for %s — using 0", s3_key)
        cloudfront_url = f"{cloudfront_base}/{playlist_id}/episodes/{video_id}.mp3"

        episodes.append(
            EpisodeMeta(
                video_id=entry.video_id,
                title=entry.title,
                description=entry.description,
                duration=entry.duration,
                upload_date=entry.upload_date,
                thumbnail=entry.thumbnail,
                webpage_url=entry.webpage_url,
                playlist_index=entry.playlist_index,
                s3_key=s3_key,
                file_size=file_size,
                cloudfront_url=cloudfront_url,
                chapters=entry.chapters if hasattr(entry, "chapters") else [],
                ads_removed=video_id in (ads_removed_ids or set()),
            )
        )

    # Sort by upload_date descending (newest first)
    episodes.sort(key=lambda e: e.upload_date, reverse=True)

    logger.info("Built metadata for %d episodes (deduplicated from %d keys)", len(episodes), len(final_keys))
    return episodes
