"""Unit tests for the RSS generator module."""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from unittest.mock import MagicMock

import pytest

from models import EpisodeMeta, PlaylistMeta, VideoEntry
from rss_generator import (
    ITUNES_NS,
    _first_paragraph,
    _format_duration,
    build_episode_metadata,
    generate_rss,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CLOUDFRONT_BASE = "https://cdn.example.com"
PLAYLIST_ID = "PLtest123"


def _make_playlist_meta(**overrides) -> PlaylistMeta:
    defaults = dict(
        title="Test Podcast",
        description="A test podcast description",
        uploader="Test Uploader",
        channel_url="https://youtube.com/c/test",
        webpage_url="https://youtube.com/playlist?list=PLtest123",
        playlist_id=PLAYLIST_ID,
    )
    defaults.update(overrides)
    return PlaylistMeta(**defaults)


def _make_episode(
    video_id: str = "vid001",
    upload_date: str = "20250601",
    duration: int = 3661,
    **overrides,
) -> EpisodeMeta:
    defaults = dict(
        video_id=video_id,
        title=f"Episode {video_id}",
        description="First paragraph.\n\nSecond paragraph.",
        duration=duration,
        upload_date=upload_date,
        thumbnail=f"https://img.youtube.com/vi/{video_id}/0.jpg",
        webpage_url=f"https://youtube.com/watch?v={video_id}",
        playlist_index=1,
        s3_key=f"{PLAYLIST_ID}/episodes/{video_id}.mp3",
        file_size=5_000_000,
        cloudfront_url=f"{CLOUDFRONT_BASE}/{PLAYLIST_ID}/episodes/{video_id}.mp3",
    )
    defaults.update(overrides)
    return EpisodeMeta(**defaults)


# ---------------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------------


class TestFormatDuration:
    def test_hours_minutes_seconds(self):
        assert _format_duration(3661) == "1:01:01"

    def test_minutes_seconds(self):
        assert _format_duration(125) == "2:05"

    def test_zero(self):
        assert _format_duration(0) == "0:00"

    def test_none(self):
        assert _format_duration(None) == "0:00"

    def test_exact_hour(self):
        assert _format_duration(3600) == "1:00:00"

    def test_under_minute(self):
        assert _format_duration(45) == "0:45"


# ---------------------------------------------------------------------------
# _first_paragraph
# ---------------------------------------------------------------------------


class TestFirstParagraph:
    def test_splits_on_double_newline(self):
        assert _first_paragraph("Hello world.\n\nMore text.") == "Hello world."

    def test_no_double_newline(self):
        assert _first_paragraph("Just one paragraph") == "Just one paragraph"

    def test_empty_string(self):
        assert _first_paragraph("") == ""

    def test_strips_whitespace(self):
        assert _first_paragraph("  Hello  \n\nMore") == "Hello"


# ---------------------------------------------------------------------------
# generate_rss — XML structure
# ---------------------------------------------------------------------------


class TestGenerateRssStructure:
    """Test that generate_rss produces well-formed RSS 2.0 XML."""

    def test_output_is_valid_xml(self):
        meta = _make_playlist_meta()
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        # Should parse without error
        root = ET.fromstring(xml_str)
        assert root.tag == "rss"
        assert root.get("version") == "2.0"

    def test_itunes_namespace_present(self):
        meta = _make_playlist_meta()
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        assert "xmlns:itunes" in xml_str
        assert ITUNES_NS in xml_str

    def test_channel_element_exists(self):
        meta = _make_playlist_meta()
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        channel = root.find("channel")
        assert channel is not None

    def test_empty_episodes_produces_valid_feed(self):
        meta = _make_playlist_meta()
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        items = root.findall(".//item")
        assert len(items) == 0


# ---------------------------------------------------------------------------
# generate_rss — channel metadata
# ---------------------------------------------------------------------------


class TestGenerateRssChannelMetadata:
    """Test channel-level metadata elements."""

    def test_channel_title(self):
        meta = _make_playlist_meta(title="My Podcast")
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/title").text == "My Podcast"

    def test_channel_link(self):
        meta = _make_playlist_meta(
            webpage_url="https://youtube.com/playlist?list=PLtest123"
        )
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/link").text == "https://youtube.com/playlist?list=PLtest123"

    def test_channel_description(self):
        meta = _make_playlist_meta(description="Great podcast")
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/description").text == "Great podcast"

    def test_channel_language(self):
        meta = _make_playlist_meta()
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/language").text == "en"

    def test_channel_generator(self):
        meta = _make_playlist_meta()
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/generator").text == "yt-podcast-lambda"

    def test_channel_last_build_date(self):
        meta = _make_playlist_meta()
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        lbd = root.find(".//channel/lastBuildDate")
        assert lbd is not None
        assert lbd.text is not None

    def test_itunes_author(self):
        meta = _make_playlist_meta(uploader="Test Author")
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        assert root.find(".//channel/itunes:author", ns).text == "Test Author"

    def test_itunes_summary(self):
        meta = _make_playlist_meta(description="Summary text")
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        assert root.find(".//channel/itunes:summary", ns).text == "Summary text"

    def test_itunes_explicit(self):
        meta = _make_playlist_meta()
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        assert root.find(".//channel/itunes:explicit", ns).text == "no"

    def test_itunes_owner(self):
        meta = _make_playlist_meta(uploader="Owner Name")
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        owner = root.find(".//channel/itunes:owner", ns)
        assert owner is not None
        assert owner.find("itunes:name", ns).text == "Owner Name"

    def test_channel_image_from_first_episode(self):
        meta = _make_playlist_meta()
        ep = _make_episode(thumbnail="https://img.youtube.com/thumb.jpg")
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        img = root.find(".//channel/itunes:image", ns)
        assert img is not None
        assert img.get("href") == "https://img.youtube.com/thumb.jpg"

    def test_no_channel_image_when_no_episodes(self):
        meta = _make_playlist_meta()
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        img = root.find(".//channel/itunes:image", ns)
        assert img is None


# ---------------------------------------------------------------------------
# generate_rss — item elements
# ---------------------------------------------------------------------------


class TestGenerateRssItems:
    """Test item-level elements."""

    def test_item_count_matches_episodes(self):
        meta = _make_playlist_meta()
        episodes = [_make_episode(video_id=f"v{i}") for i in range(3)]
        xml_str = generate_rss(meta, episodes, CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        items = root.findall(".//item")
        assert len(items) == 3

    def test_item_title(self):
        meta = _make_playlist_meta()
        ep = _make_episode(title="My Episode Title")
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//item/title").text == "My Episode Title"

    def test_item_guid(self):
        meta = _make_playlist_meta()
        ep = _make_episode(video_id="abc123")
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        guid = root.find(".//item/guid")
        assert guid.text == "abc123"
        assert guid.get("isPermaLink") == "false"

    def test_item_enclosure(self):
        meta = _make_playlist_meta()
        ep = _make_episode(video_id="abc123", file_size=1234567)
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        enc = root.find(".//item/enclosure")
        assert enc.get("url") == f"{CLOUDFRONT_BASE}/{PLAYLIST_ID}/episodes/abc123.mp3"
        assert enc.get("length") == "1234567"
        assert enc.get("type") == "audio/mpeg"

    def test_item_pubdate_rfc2822(self):
        meta = _make_playlist_meta()
        ep = _make_episode(upload_date="20250115")
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        pub_date_text = root.find(".//item/pubDate").text
        # Should be parseable as RFC 2822
        parsed = parsedate_to_datetime(pub_date_text)
        assert parsed.year == 2025
        assert parsed.month == 1
        assert parsed.day == 15

    def test_item_description_first_paragraph(self):
        meta = _make_playlist_meta()
        ep = _make_episode(description="First para.\n\nSecond para.")
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//item/description").text == "First para."

    def test_item_itunes_duration(self):
        meta = _make_playlist_meta()
        ep = _make_episode(duration=3661)
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        assert root.find(".//item/itunes:duration", ns).text == "1:01:01"

    def test_item_itunes_explicit(self):
        meta = _make_playlist_meta()
        ep = _make_episode()
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        assert root.find(".//item/itunes:explicit", ns).text == "no"

    def test_item_itunes_image(self):
        meta = _make_playlist_meta()
        ep = _make_episode(thumbnail="https://img.youtube.com/thumb.jpg")
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        img = root.find(".//item/itunes:image", ns)
        assert img is not None
        assert img.get("href") == "https://img.youtube.com/thumb.jpg"

    def test_item_itunes_episode(self):
        meta = _make_playlist_meta()
        ep = _make_episode(playlist_index=5)
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        assert root.find(".//item/itunes:episode", ns).text == "5"


# ---------------------------------------------------------------------------
# build_episode_metadata
# ---------------------------------------------------------------------------


class TestBuildEpisodeMetadata:
    """Test build_episode_metadata function."""

    def _make_video_entry(self, video_id: str, upload_date: str = "20250601") -> VideoEntry:
        return VideoEntry(
            video_id=video_id,
            title=f"Video {video_id}",
            description="desc",
            duration=300,
            upload_date=upload_date,
            thumbnail="https://img.youtube.com/thumb.jpg",
            webpage_url=f"https://youtube.com/watch?v={video_id}",
            playlist_index=1,
        )

    def test_builds_metadata_for_matching_entries(self):
        entries = [self._make_video_entry("v1"), self._make_video_entry("v2")]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 5_000_000

        result = build_episode_metadata(
            entries, {"v1", "v2"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3
        )

        assert len(result) == 2
        ids = {e.video_id for e in result}
        assert ids == {"v1", "v2"}

    def test_skips_keys_without_matching_entry(self):
        entries = [self._make_video_entry("v1")]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 5_000_000

        result = build_episode_metadata(
            entries, {"v1", "v_unknown"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3
        )

        assert len(result) == 1
        assert result[0].video_id == "v1"

    def test_sorted_newest_first(self):
        entries = [
            self._make_video_entry("old", upload_date="20250101"),
            self._make_video_entry("new", upload_date="20250601"),
            self._make_video_entry("mid", upload_date="20250301"),
        ]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 5_000_000

        result = build_episode_metadata(
            entries, {"old", "new", "mid"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3
        )

        assert [e.video_id for e in result] == ["new", "mid", "old"]

    def test_cloudfront_url_pattern(self):
        entries = [self._make_video_entry("v1")]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 1234

        result = build_episode_metadata(
            entries, {"v1"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3
        )

        assert result[0].cloudfront_url == f"{CLOUDFRONT_BASE}/{PLAYLIST_ID}/episodes/v1.mp3"
        assert result[0].s3_key == f"{PLAYLIST_ID}/episodes/v1.mp3"
        assert result[0].file_size == 1234

    def test_empty_final_keys(self):
        entries = [self._make_video_entry("v1")]
        mock_s3 = MagicMock()

        result = build_episode_metadata(
            entries, set(), CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3
        )

        assert result == []
