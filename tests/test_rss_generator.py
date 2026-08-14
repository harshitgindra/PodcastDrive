"""Unit tests for the RSS generator module."""

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from unittest.mock import MagicMock

import pytest

from models import EpisodeMeta, PlaylistMeta, VideoEntry
from rss_generator import (
    ITUNES_NS,
    _first_paragraph,
    _format_duration,
    _validate_cloudfront_base,
    build_episode_metadata,
    generate_rss,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CLOUDFRONT_BASE = "https://cdn.example.com"
PLAYLIST_ID = "PLtest123"


def _make_playlist_meta(**overrides) -> PlaylistMeta:
    defaults = {
        "title": "Test Podcast",
        "description": "A test podcast description",
        "uploader": "Test Uploader",
        "channel_url": "https://youtube.com/c/test",
        "webpage_url": "https://youtube.com/playlist?list=PLtest123",
        "playlist_id": PLAYLIST_ID,
        "thumbnail": "",
    }
    defaults.update(overrides)
    return PlaylistMeta(**defaults)


def _make_episode(
    video_id: str = "vid001",
    upload_date: str = "20250601",
    duration: int = 3661,
    **overrides,
) -> EpisodeMeta:
    defaults = {
        "video_id": video_id,
        "title": f"Episode {video_id}",
        "description": "First paragraph.\n\nSecond paragraph.",
        "duration": duration,
        "upload_date": upload_date,
        "thumbnail": f"https://img.youtube.com/vi/{video_id}/0.jpg",
        "webpage_url": f"https://youtube.com/watch?v={video_id}",
        "playlist_index": 1,
        "s3_key": f"{PLAYLIST_ID}/episodes/{video_id}.mp3",
        "file_size": 5_000_000,
        "cloudfront_url": f"{CLOUDFRONT_BASE}/{PLAYLIST_ID}/episodes/{video_id}.mp3",
    }
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
# _validate_cloudfront_base
# ---------------------------------------------------------------------------


class TestValidateCloudfrontBase:
    def test_valid_url_passes(self):
        _validate_cloudfront_base("https://cdn.example.com")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_cloudfront_base("")

    def test_http_raises(self):
        with pytest.raises(ValueError, match="https://"):
            _validate_cloudfront_base("http://cdn.example.com")

    def test_missing_scheme_raises(self):
        with pytest.raises(ValueError, match="https://"):
            _validate_cloudfront_base("cdn.example.com")

    def test_trailing_slash_raises(self):
        with pytest.raises(ValueError, match="trailing slash"):
            _validate_cloudfront_base("https://cdn.example.com/")

    def test_generate_rss_raises_on_bad_cloudfront(self):
        meta = _make_playlist_meta()
        with pytest.raises(ValueError):
            generate_rss(meta, [], "http://bad.example.com", PLAYLIST_ID)

    def test_generate_rss_raises_on_trailing_slash(self):
        meta = _make_playlist_meta()
        with pytest.raises(ValueError):
            generate_rss(meta, [], "https://cdn.example.com/", PLAYLIST_ID)


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

    def test_channel_title(self, monkeypatch):
        monkeypatch.setenv("FEED_TITLE_SUFFIX", "")
        meta = _make_playlist_meta(title="My Podcast")
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/title").text == "My Podcast"

    def test_channel_link(self):
        meta = _make_playlist_meta(webpage_url="https://youtube.com/playlist?list=PLtest123")
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
        assert root.find(".//channel/generator").text == "PodcastDrive"

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

    # --- Standard RSS 2.0 <image> element tests ---

    def test_rss_image_element_present_with_playlist_thumbnail(self, monkeypatch):
        """Standard <image> block should be emitted when PlaylistMeta.thumbnail is set."""
        monkeypatch.setenv("FEED_TITLE_SUFFIX", "")
        meta = _make_playlist_meta(thumbnail="https://example.com/playlist_art.jpg")
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        rss_img = root.find(".//channel/image")
        assert rss_img is not None
        assert rss_img.find("url").text == "https://example.com/playlist_art.jpg"
        assert rss_img.find("title").text == meta.title
        assert rss_img.find("link").text == meta.webpage_url

    def test_rss_image_element_absent_when_no_artwork(self):
        """Standard <image> block must NOT be emitted when there is no artwork at all."""
        meta = _make_playlist_meta(thumbnail="")
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/image") is None

    def test_playlist_thumbnail_preferred_over_episode_thumbnail(self):
        """PlaylistMeta.thumbnail should be used in preference to episode thumbnail."""
        meta = _make_playlist_meta(thumbnail="https://example.com/playlist_art.jpg")
        ep = _make_episode(thumbnail="https://img.youtube.com/vi/vid001/0.jpg")
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        # Both standard and itunes image should use the playlist-level thumbnail
        rss_img = root.find(".//channel/image")
        assert rss_img.find("url").text == "https://example.com/playlist_art.jpg"
        itunes_img = root.find(".//channel/itunes:image", ns)
        assert itunes_img.get("href") == "https://example.com/playlist_art.jpg"

    def test_episode_thumbnail_fallback_when_no_playlist_thumbnail(self):
        """Fall back to first episode thumbnail when PlaylistMeta.thumbnail is empty."""
        meta = _make_playlist_meta(thumbnail="")
        ep = _make_episode(thumbnail="https://img.youtube.com/vi/vid001/0.jpg")
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        rss_img = root.find(".//channel/image")
        assert rss_img is not None
        assert rss_img.find("url").text == "https://img.youtube.com/vi/vid001/0.jpg"
        itunes_img = root.find(".//channel/itunes:image", ns)
        assert itunes_img.get("href") == "https://img.youtube.com/vi/vid001/0.jpg"

    def test_itunes_image_uses_playlist_thumbnail_when_set(self):
        """`<itunes:image>` should also reflect PlaylistMeta.thumbnail."""
        meta = _make_playlist_meta(thumbnail="https://example.com/playlist_art.jpg")
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        img = root.find(".//channel/itunes:image", ns)
        assert img is not None
        assert img.get("href") == "https://example.com/playlist_art.jpg"


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
        desc = root.find(".//item/description").text
        assert desc.startswith("First para.")
        assert "Source: https://youtube.com/watch?v=vid001" in desc

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

    def test_ads_removed_appends_default_suffix(self):
        """When ads_removed=True the default ✂️ suffix is appended to the title."""
        meta = _make_playlist_meta()
        ep = _make_episode(title="Great Show", ads_removed=True)
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//item/title").text == "Great Show ✂️"

    def test_ads_removed_custom_suffix(self, monkeypatch):
        """EPISODE_AD_REMOVED_SUFFIX env var customises the suffix."""
        monkeypatch.setenv("EPISODE_AD_REMOVED_SUFFIX", " [clean]")
        meta = _make_playlist_meta()
        ep = _make_episode(title="Great Show", ads_removed=True)
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//item/title").text == "Great Show [clean]"

    def test_ads_removed_empty_suffix_no_change(self, monkeypatch):
        """When EPISODE_AD_REMOVED_SUFFIX is empty the title is unchanged."""
        monkeypatch.setenv("EPISODE_AD_REMOVED_SUFFIX", "")
        meta = _make_playlist_meta()
        ep = _make_episode(title="Great Show", ads_removed=True)
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        assert root.find(".//item/title").text == "Great Show"

    def test_summary_prepended_to_description(self):
        """When episode.summary is set it should be prepended to the description."""
        meta = _make_playlist_meta()
        ep = _make_episode(description="Original desc.", summary="AI summary here.")
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        desc = root.find(".//item/description").text
        assert desc.startswith("AI summary here.")
        assert "Original desc." in desc


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

        result = build_episode_metadata(entries, {"v1", "v2"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3)

        assert len(result) == 2
        ids = {e.video_id for e in result}
        assert ids == {"v1", "v2"}

    def test_skips_keys_without_matching_entry(self):
        entries = [self._make_video_entry("v1")]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 5_000_000

        result = build_episode_metadata(entries, {"v1", "v_unknown"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3)

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

        result = build_episode_metadata(entries, {"old", "new", "mid"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3)

        assert [e.video_id for e in result] == ["new", "mid", "old"]

    def test_cloudfront_url_pattern(self):
        entries = [self._make_video_entry("v1")]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 1234

        result = build_episode_metadata(entries, {"v1"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3)

        assert result[0].cloudfront_url == f"{CLOUDFRONT_BASE}/{PLAYLIST_ID}/episodes/v1.mp3"
        assert result[0].s3_key == f"{PLAYLIST_ID}/episodes/v1.mp3"
        assert result[0].file_size == 1234

    def test_empty_final_keys(self):
        entries = [self._make_video_entry("v1")]
        mock_s3 = MagicMock()

        result = build_episode_metadata(entries, set(), CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3)

        assert result == []

    def test_deduplicates_by_title(self):
        """Episodes with identical titles (different video_ids) are deduplicated."""
        entries = [
            VideoEntry(
                video_id="v1",
                title="My Episode",
                description="",
                duration=100,
                upload_date="20250101",
                thumbnail="",
                webpage_url="",
                playlist_index=1,
            ),
            VideoEntry(
                video_id="v2",
                title="My Episode",
                description="",
                duration=100,
                upload_date="20250101",
                thumbnail="",
                webpage_url="",
                playlist_index=2,
            ),
        ]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 1000

        result = build_episode_metadata(entries, {"v1", "v2"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3)

        assert len(result) == 1

    def test_deduplication_is_case_insensitive(self):
        """Title deduplication normalises case before comparing."""
        entries = [
            VideoEntry(
                video_id="v1",
                title="My Episode",
                description="",
                duration=100,
                upload_date="20250101",
                thumbnail="",
                webpage_url="",
                playlist_index=1,
            ),
            VideoEntry(
                video_id="v2",
                title="MY EPISODE",
                description="",
                duration=100,
                upload_date="20250101",
                thumbnail="",
                webpage_url="",
                playlist_index=2,
            ),
        ]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 1000

        result = build_episode_metadata(entries, {"v1", "v2"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3)

        assert len(result) == 1

    def test_unique_titles_all_included(self):
        """Episodes with distinct titles are all included."""
        entries = [
            VideoEntry(
                video_id=f"v{i}",
                title=f"Episode {i}",
                description="",
                duration=100,
                upload_date="20250101",
                thumbnail="",
                webpage_url="",
                playlist_index=i,
            )
            for i in range(3)
        ]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 1000

        result = build_episode_metadata(entries, {"v0", "v1", "v2"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3)

        assert len(result) == 3

    def test_manifest_upload_date_used_when_entry_date_empty(self):
        """When an entry has an empty upload_date, the manifest date is used."""
        entries = [
            VideoEntry(
                video_id="v1",
                title="No Date Episode",
                description="",
                duration=100,
                upload_date="",  # empty — flat extraction
                thumbnail="",
                webpage_url="",
                playlist_index=1,
            )
        ]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 500
        manifest = {"v1": {"upload_date": "20240315"}}

        result = build_episode_metadata(
            entries,
            {"v1"},
            CLOUDFRONT_BASE,
            PLAYLIST_ID,
            mock_s3,
            manifest=manifest,
        )

        assert len(result) == 1
        assert result[0].upload_date == "20240315"

    def test_manifest_upload_date_not_applied_when_entry_date_already_set(self):
        """When the entry already has a date, the manifest date is ignored."""
        entries = [
            VideoEntry(
                video_id="v1",
                title="Has Date Episode",
                description="",
                duration=100,
                upload_date="20230101",
                thumbnail="",
                webpage_url="",
                playlist_index=1,
            )
        ]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 500
        manifest = {"v1": {"upload_date": "20240315"}}

        result = build_episode_metadata(
            entries,
            {"v1"},
            CLOUDFRONT_BASE,
            PLAYLIST_ID,
            mock_s3,
            manifest=manifest,
        )

        assert result[0].upload_date == "20230101"

    def test_file_size_zero_on_s3_exception(self):
        """When get_object_size raises, file_size falls back to 0."""
        entries = [self._make_video_entry("v1")]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.side_effect = Exception("S3 error")

        result = build_episode_metadata(entries, {"v1"}, CLOUDFRONT_BASE, PLAYLIST_ID, mock_s3)

        assert len(result) == 1
        assert result[0].file_size == 0

    def test_summary_read_from_manifest(self):
        """When the manifest has a 'summary' key for a video_id it is set on EpisodeMeta."""
        entries = [self._make_video_entry("v1")]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 1000
        manifest = {"v1": {"summary": "A great AI-generated summary."}}

        result = build_episode_metadata(
            entries,
            {"v1"},
            CLOUDFRONT_BASE,
            PLAYLIST_ID,
            mock_s3,
            manifest=manifest,
        )

        assert result[0].summary == "A great AI-generated summary."

    def test_summary_empty_when_manifest_has_no_summary(self):
        """When the manifest exists but has no 'summary' key, EpisodeMeta.summary is ''."""
        entries = [self._make_video_entry("v1")]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 1000
        manifest = {"v1": {"upload_date": "20240101"}}  # no summary key

        result = build_episode_metadata(
            entries,
            {"v1"},
            CLOUDFRONT_BASE,
            PLAYLIST_ID,
            mock_s3,
            manifest=manifest,
        )

        assert result[0].summary == ""

    def test_summary_empty_when_no_manifest(self):
        """When manifest is None, EpisodeMeta.summary is ''."""
        entries = [self._make_video_entry("v1")]
        mock_s3 = MagicMock()
        mock_s3.get_object_size.return_value = 1000

        result = build_episode_metadata(
            entries,
            {"v1"},
            CLOUDFRONT_BASE,
            PLAYLIST_ID,
            mock_s3,
        )

        assert result[0].summary == ""


# ---------------------------------------------------------------------------
# Property-based tests (hypothesis)
# ---------------------------------------------------------------------------

from hypothesis import given, settings
from hypothesis import strategies as st


class TestFormatDurationProperty:
    @given(st.integers(min_value=1, max_value=86400))
    def test_always_contains_colon(self, seconds):
        result = _format_duration(seconds)
        assert ":" in result

    @given(st.integers(min_value=1, max_value=86400))
    def test_all_parts_are_numeric(self, seconds):
        result = _format_duration(seconds)
        parts = result.split(":")
        for part in parts:
            assert part.isdigit(), f"Non-numeric part {part!r} in {result!r}"

    @given(st.integers(min_value=3600, max_value=86400))
    def test_has_three_parts_for_one_hour_plus(self, seconds):
        result = _format_duration(seconds)
        assert len(result.split(":")) == 3

    @given(st.integers(min_value=1, max_value=3599))
    def test_has_two_parts_under_one_hour(self, seconds):
        result = _format_duration(seconds)
        assert len(result.split(":")) == 2

    @given(st.one_of(st.none(), st.integers(max_value=0)))
    def test_zero_or_none_returns_zero_zero(self, seconds):
        assert _format_duration(seconds) == "0:00"


class TestFirstParagraphProperty:
    @given(st.text(max_size=200))
    def test_result_is_prefix_of_input(self, text):
        result = _first_paragraph(text)
        # The result should always be a substring that starts from the beginning
        assert text.startswith(result) or result == text.strip().split("\n\n")[0].strip()

    @given(st.text(max_size=200))
    def test_never_contains_double_newline(self, text):
        result = _first_paragraph(text)
        assert "\n\n" not in result

    @given(st.text(max_size=50).filter(lambda t: "\n\n" not in t))
    def test_text_without_double_newline_returns_stripped_text(self, text):
        result = _first_paragraph(text)
        assert result == text.strip()


class TestGenerateRssProperty:
    @given(
        title=st.text(
            min_size=1,
            max_size=80,
            alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="<>&\x00"),
        ),
        n_episodes=st.integers(min_value=0, max_value=10),
    )
    @settings(max_examples=30)
    def test_output_is_valid_xml(self, title, n_episodes):
        meta = _make_playlist_meta(title=title)
        episodes = [_make_episode(video_id=f"v{i}", upload_date="20250601") for i in range(n_episodes)]
        xml_str = generate_rss(meta, episodes, CLOUDFRONT_BASE, PLAYLIST_ID)
        # Should parse without error
        root = ET.fromstring(xml_str)
        assert root.tag == "rss"

    @given(n_episodes=st.integers(min_value=0, max_value=15))
    @settings(max_examples=20)
    def test_episode_count_matches_items(self, n_episodes):
        meta = _make_playlist_meta()
        episodes = [_make_episode(video_id=f"v{i}", upload_date="20250601") for i in range(n_episodes)]
        xml_str = generate_rss(meta, episodes, CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        channel = root.find("channel")
        items = channel.findall("item")
        assert len(items) == n_episodes

    @given(
        video_id=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz0123456789"),
        duration=st.integers(min_value=1, max_value=7200),
    )
    @settings(max_examples=30)
    def test_enclosure_url_contains_video_id(self, video_id, duration):
        meta = _make_playlist_meta()
        ep = _make_episode(video_id=video_id, duration=duration)
        xml_str = generate_rss(meta, [ep], CLOUDFRONT_BASE, PLAYLIST_ID)
        assert video_id in xml_str
