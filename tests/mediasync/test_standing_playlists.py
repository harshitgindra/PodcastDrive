"""Tests for mediasync.standing_playlists module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mediasync.config import Config, Profile
from mediasync.notion_client import Format, MediaEntry, Status
from mediasync.standing_playlists import (
    DEFAULT_RECENT_COUNT,
    _entries_to_playlist_items,
    _generate_for_profile,
    generate_standing_playlists,
)


@pytest.fixture
def config(tmp_path):
    return Config(
        notion_token="tok",
        notion_database_id="db",
        profiles=[Profile("harshit"), Profile("spouse")],
        storage_backend="s3",
        s3_bucket="test-bucket",
        output_dir=str(tmp_path),
    )


@pytest.fixture
def mock_notion():
    return MagicMock()


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.upload.return_value = "key"
    return storage


def _make_entry(profile="harshit", file_key="MediaSync/harshit/audio/Song.m4a", url="https://youtube.com/watch?v=1"):
    return MediaEntry(
        page_id="page1",
        url=url,
        profile=profile,
        format=Format.AUDIO,
        status=Status.DONE,
        delete=False,
        file_key=file_key,
    )


class TestGenerateStandingPlaylists:
    def test_generates_for_all_profiles(self, config, mock_notion, mock_storage):
        mock_notion.get_done_for_profile.return_value = [_make_entry()]
        count = generate_standing_playlists(config, mock_notion, mock_storage)
        # 2 profiles x 2 playlists (All + Recent) = up to 4, but only 1 profile has entries
        # Actually both profiles get queried but spouse has entries too since we return same
        assert count >= 2

    def test_skips_profiles_with_no_entries(self, config, mock_notion, mock_storage):
        mock_notion.get_done_for_profile.return_value = []
        count = generate_standing_playlists(config, mock_notion, mock_storage)
        assert count == 0

    def test_handles_error_gracefully(self, config, mock_notion, mock_storage):
        mock_notion.get_done_for_profile.side_effect = Exception("API error")
        # Should not raise
        count = generate_standing_playlists(config, mock_notion, mock_storage)
        assert count == 0


class TestGenerateForProfile:
    def test_uploads_all_and_recent(self, config, mock_notion, mock_storage):
        entries = [_make_entry(file_key=f"MediaSync/harshit/audio/Song{i}.m4a") for i in range(5)]
        mock_notion.get_done_for_profile.return_value = entries

        count = _generate_for_profile("harshit", config, mock_notion, mock_storage)

        assert count == 2
        assert mock_storage.upload.call_count == 2
        # Check filenames
        upload_calls = mock_storage.upload.call_args_list
        filenames = [call[0][2] for call in upload_calls]
        assert "All.m3u8" in filenames
        assert "Recent.m3u8" in filenames

    def test_no_entries_with_file_keys(self, config, mock_notion, mock_storage):
        entries = [_make_entry(file_key="")]
        mock_notion.get_done_for_profile.return_value = entries

        count = _generate_for_profile("harshit", config, mock_notion, mock_storage)
        assert count == 0

    def test_recent_limited_to_default_count(self, config, mock_notion, mock_storage, tmp_path):
        entries = [
            _make_entry(file_key=f"MediaSync/harshit/audio/Song{i}.m4a", url=f"https://youtube.com/watch?v={i}")
            for i in range(DEFAULT_RECENT_COUNT + 20)
        ]
        mock_notion.get_done_for_profile.return_value = entries

        # Capture the file content before it's deleted by intercepting upload
        captured_content = {}
        def capture_upload(path, folder, filename):
            captured_content[filename] = path.read_text()
            return f"{folder}/{filename}"
        mock_storage.upload.side_effect = capture_upload

        count = _generate_for_profile("harshit", config, mock_notion, mock_storage)
        assert count == 2

        # Recent should have exactly DEFAULT_RECENT_COUNT EXTINF lines
        recent_content = captured_content["Recent.m3u8"]
        extinf_count = recent_content.count("#EXTINF:")
        assert extinf_count == DEFAULT_RECENT_COUNT

        # All should have all entries
        all_content = captured_content["All.m3u8"]
        all_extinf_count = all_content.count("#EXTINF:")
        assert all_extinf_count == DEFAULT_RECENT_COUNT + 20


class TestEntriesToPlaylistItems:
    def test_converts_entries(self):
        entries = [_make_entry(file_key="MediaSync/harshit/audio/My Song.m4a")]
        items = _entries_to_playlist_items(entries, "MediaSync/harshit/playlists")
        assert len(items) == 1
        assert items[0]["title"] == "My Song"
        assert items[0]["remote_key"] == "../audio/My Song.m4a"

    def test_multiple_file_keys(self):
        entries = [_make_entry(file_key="MediaSync/harshit/audio/Song.m4a; MediaSync/harshit/video/Song.mp4")]
        items = _entries_to_playlist_items(entries, "MediaSync/harshit/playlists")
        assert len(items) == 2

    def test_empty_file_key_skipped(self):
        entries = [_make_entry(file_key="")]
        items = _entries_to_playlist_items(entries, "MediaSync/harshit/playlists")
        assert len(items) == 0