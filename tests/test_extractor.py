"""Unit tests for the playlist extractor module."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from extractor import extract_playlist
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
