"""RSS 2.0 feed generator with iTunes namespace extensions.

Builds a podcast-compatible RSS feed from playlist metadata and episode
information, using CloudFront URLs for audio enclosures.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
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


def generate_rss(
    playlist_meta: PlaylistMeta,
    episodes: list[EpisodeMeta],
    cloudfront_base: str,
    playlist_id: str,
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
    """
    rss = ET.Element("rss", version="2.0")
    # The xmlns:itunes attribute is added automatically by ET.register_namespace
    # when itunes-namespaced elements are serialized. No manual set needed.

    channel = ET.SubElement(rss, "channel")

    # --- Channel-level metadata ---
    _add_channel_metadata(channel, playlist_meta, episodes, cloudfront_base, playlist_id)

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
    ET.SubElement(channel, "title").text = meta.title
    ET.SubElement(channel, "link").text = meta.webpage_url or meta.channel_url
    ET.SubElement(channel, "description").text = meta.description or meta.title
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "generator").text = "yt-podcast-lambda"

    now = datetime.now(timezone.utc)
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)

    # iTunes tags
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = meta.uploader or meta.title
    ET.SubElement(channel, f"{{{ITUNES_NS}}}summary").text = meta.description or meta.title
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = "no"

    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text = meta.uploader or meta.title

    # Channel image from first episode thumbnail (if available)
    if episodes and episodes[0].thumbnail:
        img = ET.SubElement(channel, f"{{{ITUNES_NS}}}image")
        img.set("href", episodes[0].thumbnail)


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

    ET.SubElement(item, "title").text = episode.title

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

    # Description: first paragraph of YouTube description
    desc_text = _first_paragraph(episode.description)
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

    for video_id in final_keys:
        entry = entry_map.get(video_id)
        if not entry:
            # MP3 in S3 but not in current playlist extraction — skip
            logger.warning(
                "Video %s in S3 but not in playlist extraction; skipping",
                video_id,
            )
            continue

        s3_key = f"{playlist_id}/episodes/{video_id}.mp3"
        file_size = s3.get_object_size(s3_key)
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
            )
        )

    # Sort by upload_date descending (newest first)
    episodes.sort(key=lambda e: e.upload_date, reverse=True)

    logger.info("Built metadata for %d episodes", len(episodes))
    return episodes
