"""Tests for playlist sync module."""

from unittest.mock import MagicMock, patch

from mediasync.notion_client import Format, MediaEntry, Status
from mediasync.playlist_sync import (
    _build_known_set,
    _extract_video_id,
    sync_playlists,
)


def _make_entry(url, profile="Test", status=Status.DONE, fmt=Format.AUDIO, file_key=""):
    return MediaEntry(
        page_id="page-1",
        url=url,
        profile=profile,
        format=fmt,
        status=status,
        delete=False,
        file_key=file_key,
    )


class TestExtractVideoId:
    def test_standard_watch_url(self):
        assert _extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_watch_url_with_params(self):
        assert _extract_video_id("https://www.youtube.com/watch?v=abc123_-XYZ&list=PL123") == "abc123_-XYZ"

    def test_short_url(self):
        assert _extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_non_youtube_url(self):
        assert _extract_video_id("https://example.com/video") == "https://example.com/video"


class TestBuildKnownSet:
    def test_extracts_ids(self):
        entries = [
            _make_entry("https://www.youtube.com/watch?v=aaa11111111"),
            _make_entry("https://www.youtube.com/watch?v=bbb22222222"),
        ]
        known = _build_known_set(entries)
        assert known == {"aaa11111111", "bbb22222222"}

    def test_includes_playlist_urls(self):
        entries = [
            _make_entry("https://www.youtube.com/playlist?list=PLxxx"),
        ]
        known = _build_known_set(entries)
        # Playlist URL has no video ID, falls back to full URL
        assert "https://www.youtube.com/playlist?list=PLxxx" in known


class TestSyncPlaylists:
    @patch("mediasync.playlist_sync.get_playlist_metadata")
    def test_creates_entries_for_new_videos(self, mock_meta):
        mock_meta.return_value = [
            {"id": "aaa11111111", "title": "Existing"},
            {"id": "bbb22222222", "title": "New Video"},
            {"id": "ccc33333333", "title": "Another New"},
        ]

        notion = MagicMock()
        # Profile has one done playlist entry and one done single video
        notion.get_all_for_profile.return_value = [
            _make_entry(
                "https://www.youtube.com/playlist?list=PLtest",
                status=Status.DONE,
            ),
            _make_entry(
                "https://www.youtube.com/watch?v=aaa11111111",
                status=Status.DONE,
            ),
        ]
        notion.create_entry.return_value = "new-page-id"

        created = sync_playlists(notion, ["Test"])

        assert created == 2
        assert notion.create_entry.call_count == 2
        # Should have created entries for bbb and ccc
        calls = notion.create_entry.call_args_list
        urls_created = {c[0][0] for c in calls}
        assert "https://www.youtube.com/watch?v=bbb22222222" in urls_created
        assert "https://www.youtube.com/watch?v=ccc33333333" in urls_created

    @patch("mediasync.playlist_sync.get_playlist_metadata")
    def test_skips_when_all_present(self, mock_meta):
        mock_meta.return_value = [
            {"id": "aaa11111111", "title": "Already There"},
        ]

        notion = MagicMock()
        notion.get_all_for_profile.return_value = [
            _make_entry("https://www.youtube.com/playlist?list=PLtest", status=Status.DONE),
            _make_entry("https://www.youtube.com/watch?v=aaa11111111", status=Status.DONE),
        ]

        created = sync_playlists(notion, ["Test"])

        assert created == 0
        notion.create_entry.assert_not_called()

    @patch("mediasync.playlist_sync.get_playlist_metadata")
    def test_handles_fetch_failure_gracefully(self, mock_meta):
        from mediasync.downloader import DownloadError
        mock_meta.side_effect = DownloadError("Network error")

        notion = MagicMock()
        notion.get_all_for_profile.return_value = [
            _make_entry("https://www.youtube.com/playlist?list=PLtest", status=Status.DONE),
        ]

        created = sync_playlists(notion, ["Test"])

        assert created == 0
        notion.create_entry.assert_not_called()

    @patch("mediasync.playlist_sync.get_playlist_metadata")
    def test_ignores_pending_playlist_entries(self, mock_meta):
        """Only sync playlists that are already done (successfully processed)."""
        notion = MagicMock()
        notion.get_all_for_profile.return_value = [
            _make_entry("https://www.youtube.com/playlist?list=PLtest", status=Status.PENDING),
        ]

        created = sync_playlists(notion, ["Test"])

        assert created == 0
        mock_meta.assert_not_called()

    @patch("mediasync.playlist_sync.get_playlist_metadata")
    def test_preserves_profile_and_format(self, mock_meta):
        """New entries inherit the profile and format from the playlist entry."""
        mock_meta.return_value = [
            {"id": "new11111111", "title": "New"},
        ]

        notion = MagicMock()
        notion.get_all_for_profile.return_value = [
            _make_entry(
                "https://www.youtube.com/playlist?list=PLtest",
                profile="Dishita",
                status=Status.DONE,
                fmt=Format.VIDEO,
            ),
        ]
        notion.create_entry.return_value = "new-page"

        sync_playlists(notion, ["Dishita"])

        notion.create_entry.assert_called_once_with(
            "https://www.youtube.com/watch?v=new11111111",
            "Dishita",
            Format.VIDEO,
        )

    @patch("mediasync.playlist_sync.get_playlist_metadata")
    def test_no_duplicates_within_same_run(self, mock_meta):
        """If a video appears in multiple playlists, only create one entry."""
        mock_meta.return_value = [
            {"id": "dup11111111", "title": "Shared"},
        ]

        notion = MagicMock()
        # Two done playlists, both containing the same video
        notion.get_all_for_profile.return_value = [
            _make_entry("https://www.youtube.com/playlist?list=PL1", status=Status.DONE),
            _make_entry("https://www.youtube.com/playlist?list=PL2", status=Status.DONE),
        ]
        notion.create_entry.return_value = "new-page"

        created = sync_playlists(notion, ["Test"])

        # Should only create one entry, not two
        assert created == 1
        assert notion.create_entry.call_count == 1
