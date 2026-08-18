"""Tests for storage reconciliation (skip download if files exist)."""

from unittest.mock import MagicMock, patch

import pytest

from mediasync.config import Config, Profile
from mediasync.notion_client import Format, MediaEntry, Status
from mediasync.pipeline import _reconcile_with_storage


@pytest.fixture
def config():
    return Config(
        notion_token="token",
        notion_database_id="db-id",
        s3_bucket="test-bucket",
        s3_region="us-west-2",
        s3_prefix="MediaSync",
        profiles=[Profile("Harshit")],
        max_duration_secs=7200,
        output_dir="/tmp/mediasync",
        herald_enabled=False,
        herald_job_id="mediasync",
    )


@pytest.fixture
def audio_entry():
    return MediaEntry(
        page_id="page-1",
        url="https://youtube.com/watch?v=abc123",
        profile="Harshit",
        format=Format.AUDIO,
        status=Status.PENDING,
        delete=False,
    )


@pytest.fixture
def video_entry():
    return MediaEntry(
        page_id="page-2",
        url="https://youtube.com/watch?v=vid456",
        profile="Harshit",
        format=Format.VIDEO,
        status=Status.PENDING,
        delete=False,
    )


@pytest.fixture
def both_entry():
    return MediaEntry(
        page_id="page-3",
        url="https://youtube.com/watch?v=both789",
        profile="Harshit",
        format=Format.BOTH,
        status=Status.PENDING,
        delete=False,
    )


class TestReconcileWithStorage:
    @patch("mediasync.pipeline.get_metadata")
    def test_returns_file_keys_when_audio_exists(self, mock_meta, audio_entry, config):
        mock_meta.return_value = {
            "title": "My Song",
            "uploader": "Artist Name",
            "duration": 240,
        }
        mock_storage = MagicMock()
        mock_storage.file_exists.return_value = True

        result = _reconcile_with_storage(
            "https://youtube.com/watch?v=abc123", audio_entry, mock_storage, config
        )

        assert result is not None
        file_keys, duration = result
        assert duration == 240
        assert len(file_keys) == 1
        assert file_keys[0] == "MediaSync/Harshit/audio/Artist Name/My Song.m4a"
        mock_storage.file_exists.assert_called_once_with(
            "MediaSync/Harshit/audio/Artist Name/My Song.m4a"
        )

    @patch("mediasync.pipeline.get_metadata")
    def test_returns_none_when_file_missing(self, mock_meta, audio_entry, config):
        mock_meta.return_value = {
            "title": "Missing Song",
            "uploader": "Artist",
            "duration": 180,
        }
        mock_storage = MagicMock()
        mock_storage.file_exists.return_value = False

        result = _reconcile_with_storage(
            "https://youtube.com/watch?v=abc123", audio_entry, mock_storage, config
        )

        assert result is None

    @patch("mediasync.pipeline.get_metadata")
    def test_video_format_checks_mp4(self, mock_meta, video_entry, config):
        mock_meta.return_value = {
            "title": "My Video",
            "uploader": "Channel",
            "duration": 600,
        }
        mock_storage = MagicMock()
        mock_storage.file_exists.return_value = True

        result = _reconcile_with_storage(
            "https://youtube.com/watch?v=vid456", video_entry, mock_storage, config
        )

        assert result is not None
        file_keys, duration = result
        assert file_keys[0] == "MediaSync/Harshit/video/Channel/My Video.mp4"
        assert duration == 600

    @patch("mediasync.pipeline.get_metadata")
    def test_both_format_checks_audio_and_video(self, mock_meta, both_entry, config):
        mock_meta.return_value = {
            "title": "Both",
            "uploader": "Creator",
            "duration": 300,
        }
        mock_storage = MagicMock()
        mock_storage.file_exists.return_value = True

        result = _reconcile_with_storage(
            "https://youtube.com/watch?v=both789", both_entry, mock_storage, config
        )

        assert result is not None
        file_keys, duration = result
        assert len(file_keys) == 2
        assert "MediaSync/Harshit/audio/Creator/Both.m4a" in file_keys
        assert "MediaSync/Harshit/video/Creator/Both.mp4" in file_keys

    @patch("mediasync.pipeline.get_metadata")
    def test_both_format_partial_exists_returns_none(self, mock_meta, both_entry, config):
        """If only audio exists but not video, return None (need to download both)."""
        mock_meta.return_value = {
            "title": "Partial",
            "uploader": "Creator",
            "duration": 300,
        }
        mock_storage = MagicMock()
        # Audio exists, video doesn't
        mock_storage.file_exists.side_effect = [True, False]

        result = _reconcile_with_storage(
            "https://youtube.com/watch?v=both789", both_entry, mock_storage, config
        )

        assert result is None

    @patch("mediasync.pipeline.get_playlist_metadata")
    @patch("mediasync.pipeline.get_metadata")
    def test_reconciles_playlist_all_exist(self, mock_meta, mock_playlist_meta, audio_entry, config):
        """Playlist reconciliation returns file_keys when all items exist on storage."""
        mock_playlist_meta.return_value = [
            {"id": "vid1", "title": "Song 1"},
            {"id": "vid2", "title": "Song 2"},
        ]
        mock_meta.side_effect = [
            {"title": "Song 1", "uploader": "Artist A", "duration": 180},
            {"title": "Song 2", "uploader": "Artist B", "duration": 240},
        ]
        mock_storage = MagicMock()
        mock_storage.file_exists.return_value = True

        result = _reconcile_with_storage(
            "https://youtube.com/playlist?list=PLxxxxx", audio_entry, mock_storage, config
        )

        assert result is not None
        file_keys, duration = result
        assert len(file_keys) == 2
        assert duration == 420

    @patch("mediasync.pipeline.get_playlist_metadata")
    @patch("mediasync.pipeline.get_metadata")
    def test_reconciles_playlist_item_missing(self, mock_meta, mock_playlist_meta, audio_entry, config):
        """Playlist reconciliation returns None when an item is missing from storage."""
        mock_playlist_meta.return_value = [
            {"id": "vid1", "title": "Song 1"},
            {"id": "vid2", "title": "Song 2"},
        ]
        mock_meta.side_effect = [
            {"title": "Song 1", "uploader": "Artist A", "duration": 180},
            {"title": "Song 2", "uploader": "Artist B", "duration": 240},
        ]
        mock_storage = MagicMock()
        # First item exists, second does not
        mock_storage.file_exists.side_effect = [True, False]

        result = _reconcile_with_storage(
            "https://youtube.com/playlist?list=PLxxxxx", audio_entry, mock_storage, config
        )

        assert result is None

    @patch("mediasync.pipeline.get_playlist_metadata")
    def test_reconciles_playlist_metadata_failure(self, mock_playlist_meta, audio_entry, config):
        """Playlist reconciliation returns None when playlist metadata fetch fails."""
        from mediasync.downloader import DownloadError
        mock_playlist_meta.side_effect = DownloadError("unavailable")
        mock_storage = MagicMock()

        result = _reconcile_with_storage(
            "https://youtube.com/playlist?list=PLxxxxx", audio_entry, mock_storage, config
        )

        assert result is None

    @patch("mediasync.pipeline.get_metadata")
    def test_returns_none_on_metadata_failure(self, mock_meta, audio_entry, config):
        from mediasync.downloader import DownloadError
        mock_meta.side_effect = DownloadError("unavailable")
        mock_storage = MagicMock()

        result = _reconcile_with_storage(
            "https://youtube.com/watch?v=abc123", audio_entry, mock_storage, config
        )

        assert result is None

    @patch("mediasync.pipeline.get_metadata")
    def test_no_group_by_channel(self, mock_meta, audio_entry):
        """Without group_by_channel, path has no channel folder."""
        from dataclasses import replace
        from mediasync.config import Config, Profile
        config = Config(
            notion_token="token",
            notion_database_id="db-id",
            s3_bucket="test-bucket",
            s3_region="us-west-2",
            s3_prefix="MediaSync",
            profiles=[Profile("Harshit")],
            max_duration_secs=7200,
            output_dir="/tmp/mediasync",
            herald_enabled=False,
            herald_job_id="mediasync",
            group_by_channel=False,
        )
        mock_meta.return_value = {
            "title": "Flat Song",
            "uploader": "Artist",
            "duration": 120,
        }
        mock_storage = MagicMock()
        mock_storage.file_exists.return_value = True

        result = _reconcile_with_storage(
            "https://youtube.com/watch?v=abc123", audio_entry, mock_storage, config
        )

        assert result is not None
        file_keys, _ = result
        assert file_keys[0] == "MediaSync/Harshit/audio/Flat Song.m4a"

    @patch("mediasync.pipeline.get_metadata")
    def test_unknown_artist_no_channel_folder(self, mock_meta, audio_entry, config):
        """Unknown artist means no channel grouping."""
        mock_meta.return_value = {
            "title": "Mystery",
            "duration": 90,
        }
        mock_storage = MagicMock()
        mock_storage.file_exists.return_value = True

        result = _reconcile_with_storage(
            "https://youtube.com/watch?v=abc123", audio_entry, mock_storage, config
        )

        assert result is not None
        file_keys, _ = result
        assert file_keys[0] == "MediaSync/Harshit/audio/Mystery.m4a"

    @patch("mediasync.pipeline.get_metadata")
    def test_sanitizes_title(self, mock_meta, audio_entry, config):
        """Titles with unsafe chars get sanitized."""
        mock_meta.return_value = {
            "title": 'Song: "Best" One?',
            "uploader": "Artist",
            "duration": 100,
        }
        mock_storage = MagicMock()
        mock_storage.file_exists.return_value = True

        result = _reconcile_with_storage(
            "https://youtube.com/watch?v=abc123", audio_entry, mock_storage, config
        )

        assert result is not None
        file_keys, _ = result
        # Colons, quotes, question marks removed
        assert file_keys[0] == "MediaSync/Harshit/audio/Artist/Song Best One.m4a"
