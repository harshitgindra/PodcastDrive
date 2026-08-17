"""Unit tests for the playlist extractor module."""

from unittest.mock import MagicMock, patch

import pytest

from extractor import (
    BotDetectedError,
    ExtractionError,
    _is_bot_detection,
    _is_permanently_unavailable,
    extract_playlist,
    extract_video_metadata,
)
from models import PlaylistMeta, VideoEntry


def _make_yt_result(
    title="Test Playlist",
    description="A test playlist",
    uploader="TestUser",
    channel_url="https://www.youtube.com/c/TestUser",
    webpage_url="https://www.youtube.com/playlist?list=PLtest123",
    entries=None,
):
    """Build a fake yt_dlp extract_info result dict."""
    return {
        "title": title,
        "description": description,
        "uploader": uploader,
        "channel_url": channel_url,
        "webpage_url": webpage_url,
        "entries": entries or [],
    }


def _make_entry(
    video_id="vid001",
    title="Video Title",
    description="Video desc",
    duration=300,
    upload_date="20250601",
    thumbnail="https://i.ytimg.com/vi/vid001/hqdefault.jpg",
    webpage_url="https://www.youtube.com/watch?v=vid001",
    playlist_index=1,
):
    """Build a fake yt_dlp entry dict."""
    return {
        "id": video_id,
        "title": title,
        "description": description,
        "duration": duration,
        "upload_date": upload_date,
        "thumbnail": thumbnail,
        "webpage_url": webpage_url,
        "playlist_index": playlist_index,
    }


PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLtest123"


class TestExtractPlaylistBasic:
    """3.1 — Basic extraction and mapping to data models."""

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_returns_playlist_meta_and_video_entries(self, mock_ydl_cls):
        entries = [_make_entry(video_id="a1"), _make_entry(video_id="a2")]
        result = _make_yt_result(entries=entries)

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        meta, videos = extract_playlist(PLAYLIST_URL)

        assert isinstance(meta, PlaylistMeta)
        assert meta.title == "Test Playlist"
        assert meta.playlist_id == "PLtest123"
        assert len(videos) == 2
        assert all(isinstance(v, VideoEntry) for v in videos)

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_maps_playlist_meta_fields(self, mock_ydl_cls):
        result = _make_yt_result(
            title="My Playlist",
            description="Desc",
            uploader="Uploader",
            channel_url="https://yt.com/c/up",
            webpage_url="https://yt.com/playlist?list=PLtest123",
        )

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        meta, _ = extract_playlist(PLAYLIST_URL)

        assert meta.title == "My Playlist"
        assert meta.description == "Desc"
        assert meta.uploader == "Uploader"
        assert meta.channel_url == "https://yt.com/c/up"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_maps_video_entry_fields(self, mock_ydl_cls):
        entry = _make_entry(
            video_id="xyz",
            title="Cool Video",
            description="Cool desc",
            duration=120,
            upload_date="20250515",
            thumbnail="https://img.com/thumb.jpg",
            webpage_url="https://yt.com/watch?v=xyz",
            playlist_index=3,
        )
        result = _make_yt_result(entries=[entry])

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        _, videos = extract_playlist(PLAYLIST_URL)

        v = videos[0]
        assert v.video_id == "xyz"
        assert v.title == "Cool Video"
        assert v.description == ""  # Flat extraction doesn't return descriptions
        assert v.duration == 120
        assert v.upload_date == ""  # Flat extraction doesn't return upload_date
        assert v.thumbnail == ""  # Flat extraction uses thumbnails list, not thumbnail field
        assert v.webpage_url == "https://www.youtube.com/watch?v=xyz"
        assert v.playlist_index == 3

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_ydl_options_configured_correctly(self, mock_ydl_cls):
        result = _make_yt_result()

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        extract_playlist(PLAYLIST_URL)

        opts = mock_ydl_cls.call_args[0][0]
        assert opts["quiet"] is True
        assert opts["no_warnings"] is True
        assert opts["extract_flat"] == "in_playlist"
        assert opts["ignoreerrors"] is True

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_calls_extract_info_with_download_false(self, mock_ydl_cls):
        result = _make_yt_result()

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        extract_playlist(PLAYLIST_URL)

        mock_ydl.extract_info.assert_called_once_with(PLAYLIST_URL, download=False)


class TestSkipUnavailableVideos:
    """3.2 — Handle unavailable/private videos by skipping None entries."""

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_skips_none_entries(self, mock_ydl_cls):
        entries = [
            _make_entry(video_id="a1"),
            None,
            _make_entry(video_id="a3"),
        ]
        result = _make_yt_result(entries=entries)

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        _, videos = extract_playlist(PLAYLIST_URL)

        assert len(videos) == 2
        assert videos[0].video_id == "a1"
        assert videos[1].video_id == "a3"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_all_none_entries_returns_empty_list(self, mock_ydl_cls):
        entries = [None, None, None]
        result = _make_yt_result(entries=entries)

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        _, videos = extract_playlist(PLAYLIST_URL)

        assert videos == []

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_empty_entries_returns_empty_list(self, mock_ydl_cls):
        result = _make_yt_result(entries=[])

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        _, videos = extract_playlist(PLAYLIST_URL)

        assert videos == []

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_none_entries_field_returns_empty_list(self, mock_ydl_cls):
        result = _make_yt_result()
        result["entries"] = None

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        _, videos = extract_playlist(PLAYLIST_URL)

        assert videos == []


class TestValidationRules:
    """3.3 — Validation: skip empty video_id, fallback title, fallback upload_date."""

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_skips_entry_with_empty_video_id(self, mock_ydl_cls):
        entries = [
            _make_entry(video_id=""),
            _make_entry(video_id="valid1"),
        ]
        result = _make_yt_result(entries=entries)

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        _, videos = extract_playlist(PLAYLIST_URL)

        assert len(videos) == 1
        assert videos[0].video_id == "valid1"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_skips_entry_with_none_video_id(self, mock_ydl_cls):
        entry = _make_entry()
        entry["id"] = None
        result = _make_yt_result(entries=[entry, _make_entry(video_id="ok")])

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        _, videos = extract_playlist(PLAYLIST_URL)

        assert len(videos) == 1
        assert videos[0].video_id == "ok"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_fallback_empty_title_to_default(self, mock_ydl_cls):
        result = _make_yt_result(title="")

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        meta, _ = extract_playlist(PLAYLIST_URL)

        assert meta.title == "YouTube Playlist Podcast"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_fallback_none_title_to_default(self, mock_ydl_cls):
        result = _make_yt_result(title=None)
        result["title"] = None

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        meta, _ = extract_playlist(PLAYLIST_URL)

        assert meta.title == "YouTube Playlist Podcast"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_fallback_whitespace_title_to_default(self, mock_ydl_cls):
        result = _make_yt_result(title="   ")

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        meta, _ = extract_playlist(PLAYLIST_URL)

        assert meta.title == "YouTube Playlist Podcast"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_upload_date_always_empty_in_flat_extraction(self, mock_ydl_cls):
        """Flat extraction doesn't return upload_date — always empty string."""
        entry = _make_entry(upload_date="20250115")
        result = _make_yt_result(entries=[entry])

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        _, videos = extract_playlist(PLAYLIST_URL)

        assert videos[0].upload_date == ""


# ---------------------------------------------------------------------------
# extract_video_metadata
# ---------------------------------------------------------------------------


class TestExtractVideoMetadata:
    """Tests for full single-video metadata extraction."""

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_returns_metadata_dict(self, mock_ydl_cls):
        info = {
            "upload_date": "20250601",
            "description": "Test desc",
            "thumbnail": "https://img.com/thumb.jpg",
            "duration": 360,
            "title": "My Video",
        }
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = info
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        result = extract_video_metadata("https://youtube.com/watch?v=abc")

        assert result is not None
        assert result["upload_date"] == "20250601"
        assert result["description"] == "Test desc"
        assert result["thumbnail"] == "https://img.com/thumb.jpg"
        assert result["duration"] == 360
        assert result["title"] == "My Video"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_raises_when_info_is_none(self, mock_ydl_cls):
        """Empty metadata is an extraction fault, not a deleted video."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = None
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        with pytest.raises(ExtractionError, match="no metadata"):
            extract_video_metadata("https://youtube.com/watch?v=missing")

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_raises_on_unexpected_exception(self, mock_ydl_cls):
        """A non-yt-dlp fault (e.g. broken pipe) must not look like a missing video."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = Exception("yt-dlp error")
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        with pytest.raises(ExtractionError, match="yt-dlp error"):
            extract_video_metadata("https://youtube.com/watch?v=error")

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_missing_fields_default_to_empty(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        # Use a non-empty dict (truthy) so the `if not info` check passes
        mock_ydl.extract_info.return_value = {"_type": "video"}  # minimal truthy dict
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        result = extract_video_metadata("https://youtube.com/watch?v=empty")
        assert result is not None
        assert result["upload_date"] == ""
        assert result["description"] == ""
        assert result["thumbnail"] == ""
        assert result["duration"] is None
        assert result["title"] == ""


class TestPermanentUnavailabilityClassifier:
    """_is_permanently_unavailable must separate dead videos from broken extraction."""

    @pytest.mark.parametrize(
        "message",
        [
            "ERROR: [youtube] abc: Video unavailable",
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
            "This video has been removed by the uploader",
            "ERROR: This video has been removed for violating YouTube's Terms of Service",
            "The account associated with this video has been terminated",
            "The uploader has not made this video available in your country",
            "Join this channel to get access to members-only content",
            "This video is available to this channel's members on level: Tier 1",
            "ERROR: Sign in to confirm your age. This video may be inappropriate for some users.",
            "ERROR: [youtube] abc: This live event has ended",
        ],
    )
    def test_permanent_reasons_recognised(self, message):
        assert _is_permanently_unavailable(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            # The exact symptom of a missing JS challenge solver.
            "ERROR: [youtube] 0MDuF2Pn2TQ: Requested format is not available. "
            "Use --list-formats for a list of available formats",
            "ERROR: unable to download video data: HTTP Error 403: Forbidden",
            "ERROR: [Errno 32] Broken pipe",
            "ERROR: Unable to download API page: HTTP Error 500: Internal Server Error",
            "ERROR: The read operation timed out",
            "ERROR: n challenge solving failed: Some formats may be missing",
        ],
    )
    def test_transient_reasons_not_treated_as_permanent(self, message):
        assert _is_permanently_unavailable(message) is False

    def test_matching_is_case_insensitive(self):
        assert _is_permanently_unavailable("VIDEO UNAVAILABLE") is True


class TestExtractVideoMetadataErrorClassification:
    """Extraction faults must be distinguishable from genuinely dead videos."""

    @staticmethod
    def _ydl(mock_ydl_cls, side_effect):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = side_effect
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl
        return mock_ydl

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_missing_format_is_an_extraction_error(self, mock_ydl_cls):
        """Regression: this used to be logged as 'video unavailable' and skipped forever."""
        import yt_dlp

        self._ydl(
            mock_ydl_cls,
            yt_dlp.utils.DownloadError(
                "ERROR: [youtube] 0MDuF2Pn2TQ: Requested format is not available."
            ),
        )

        with pytest.raises(ExtractionError, match="Requested format is not available"):
            extract_video_metadata("https://youtube.com/watch?v=0MDuF2Pn2TQ")

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_genuinely_unavailable_video_returns_none(self, mock_ydl_cls):
        import yt_dlp

        self._ydl(mock_ydl_cls, yt_dlp.utils.DownloadError("ERROR: [youtube] x: Video unavailable"))

        assert extract_video_metadata("https://youtube.com/watch?v=x") is None

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_bot_detection_still_raises_bot_detected(self, mock_ydl_cls):
        import yt_dlp

        self._ydl(
            mock_ydl_cls,
            yt_dlp.utils.DownloadError("ERROR: Sign in to confirm you're not a bot"),
        )

        with pytest.raises(BotDetectedError, match="refresh_cookies"):
            extract_video_metadata("https://youtube.com/watch?v=x")

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_bot_detected_error_from_body_propagates(self, mock_ydl_cls):
        """A BotDetectedError raised inside the try block must not be downgraded."""
        self._ydl(mock_ydl_cls, BotDetectedError("blocked"))

        with pytest.raises(BotDetectedError, match="blocked"):
            extract_video_metadata("https://youtube.com/watch?v=x")


class TestRemoteComponentsWiring:
    """Both extractor entry points must allow the JS challenge solver to be fetched."""

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_playlist_extraction_enables_remote_components(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = _make_yt_result()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        extract_playlist("https://www.youtube.com/playlist?list=PLtest123")

        opts = mock_ydl_cls.call_args[0][0]
        assert opts["remote_components"] == ["ejs:github"]

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_video_metadata_enables_remote_components(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {"_type": "video", "id": "x"}
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        extract_video_metadata("https://youtube.com/watch?v=x")

        opts = mock_ydl_cls.call_args[0][0]
        assert opts["remote_components"] == ["ejs:github"]


class TestThumbnailSelection:
    """yt-dlp lists thumbnails smallest-first, so the largest is the last one."""

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_playlist_prefers_largest_thumbnail_from_list(self, mock_ydl_cls):
        result = _make_yt_result()
        result["thumbnails"] = [
            {"url": "https://img/small.jpg"},
            {"url": "https://img/medium.jpg"},
            {"url": "https://img/large.jpg"},
        ]
        result["thumbnail"] = "https://img/fallback.jpg"
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        meta, _ = extract_playlist("https://www.youtube.com/playlist?list=PLtest123")

        assert meta.thumbnail == "https://img/large.jpg"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_playlist_falls_back_when_thumbnail_entry_has_no_url(self, mock_ydl_cls):
        result = _make_yt_result()
        result["thumbnails"] = [{"width": 120}]  # no "url" key
        result["thumbnail"] = "https://img/fallback.jpg"
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = result
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        meta, _ = extract_playlist("https://www.youtube.com/playlist?list=PLtest123")

        assert meta.thumbnail == "https://img/fallback.jpg"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_entry_prefers_largest_thumbnail_from_list(self, mock_ydl_cls):
        entry = _make_entry()
        entry["thumbnails"] = [
            {"url": "https://img/vid-small.jpg"},
            {"url": "https://img/vid-large.jpg"},
        ]
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = _make_yt_result(entries=[entry])
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        _, videos = extract_playlist("https://www.youtube.com/playlist?list=PLtest123")

        assert videos[0].thumbnail == "https://img/vid-large.jpg"

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_entry_thumbnail_empty_when_no_thumbnails_listed(self, mock_ydl_cls):
        entry = _make_entry()
        entry.pop("thumbnails", None)
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = _make_yt_result(entries=[entry])
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        _, videos = extract_playlist("https://www.youtube.com/playlist?list=PLtest123")

        assert videos[0].thumbnail == ""


# ---------------------------------------------------------------------------
# _is_bot_detection
# ---------------------------------------------------------------------------


class TestIsBotDetection:
    @pytest.mark.parametrize(
        "message",
        [
            "ERROR: Sign in to confirm you're not a bot",
            "sign in to confirm your age",
            "Please confirm you are not a bot",
            "Bot detection triggered",
            "Suspected bot activity",
            "We have detected unusual traffic from your network",
        ],
    )
    def test_real_challenges_match(self, message):
        assert _is_bot_detection(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            # "bot" appears as a substring of an ordinary English word.
            "ERROR: unable to download video: Robots.txt disallowed",
            "Requested format is not available (bottom of the list)",
            "Video unavailable: alleged sabotage",
            # yt-dlp echoes the video title back in some errors.
            "ERROR: [youtube] abc123: 'How I Built a Robot' is unavailable",
            "HTTP Error 429: Too Many Requests",
            "Requested format is not available",
            "",
        ],
    )
    def test_incidental_bot_substring_does_not_match(self, message):
        assert _is_bot_detection(message) is False

    def test_matching_is_case_insensitive(self):
        assert _is_bot_detection("SIGN IN TO CONFIRM YOU'RE NOT A BOT") is True


class TestBotDetectionFalsePositiveInMetadata:
    """A "robot" in an error message must not abort the whole playlist run."""

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_robot_in_message_is_an_extraction_error(self, mock_ydl_cls):
        import yt_dlp

        ydl = MagicMock()
        ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            "ERROR: [youtube] xyz: Requested format is not available for 'Robot Wars'"
        )
        mock_ydl_cls.return_value.__enter__.return_value = ydl

        with pytest.raises(ExtractionError):
            extract_video_metadata("https://youtube.com/watch?v=xyz")

    @patch("extractor.yt_dlp.YoutubeDL")
    def test_genuine_challenge_still_raises_bot_detected(self, mock_ydl_cls):
        import yt_dlp

        ydl = MagicMock()
        ydl.extract_info.side_effect = yt_dlp.utils.DownloadError("ERROR: Sign in to confirm you're not a bot")
        mock_ydl_cls.return_value.__enter__.return_value = ydl

        with pytest.raises(BotDetectedError):
            extract_video_metadata("https://youtube.com/watch?v=xyz")
