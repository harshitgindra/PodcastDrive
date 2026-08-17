"""Tests for mediasync.migrate module."""

from pathlib import PurePosixPath
from unittest.mock import MagicMock, patch

import pytest

from mediasync.config import Config, Profile
from mediasync.notion_client import Format, MediaEntry, Status
from mediasync.migrate import (
    _get_channel_for_url,
    _migrate_entry,
    _sanitize_channel,
    migrate,
    regenerate_playlists,
)


@pytest.fixture
def config(tmp_path):
    return Config(
        notion_token="tok",
        notion_database_id="db",
        profiles=[Profile("harshit")],
        storage_backend="onedrive",
        onedrive_client_id="id",
        onedrive_client_secret="secret",
        onedrive_refresh_token="token",
        onedrive_prefix="MediaSync",
        group_by_channel=True,
        output_dir=str(tmp_path),
        herald_enabled=False,
    )


def _make_entry(file_key="MediaSync/harshit/audio/Song.m4a", url="https://youtube.com/watch?v=abc"):
    return MediaEntry(
        page_id="page1",
        url=url,
        profile="harshit",
        format=Format.AUDIO,
        status=Status.DONE,
        delete=False,
        file_key=file_key,
    )


class TestMigrateEntry:
    def test_flat_path_gets_moved(self, config):
        entry = _make_entry(file_key="MediaSync/harshit/audio/Song.m4a")
        notion = MagicMock()
        storage = MagicMock()

        with patch("mediasync.migrate._get_channel_for_url", return_value="Cool Channel"):
            with patch("mediasync.migrate._move_file", return_value=True):
                with patch("mediasync.migrate._upload_artwork_for_entry"):
                    result = _migrate_entry(
                        entry, config, notion, storage,
                        artwork_uploaded=set(), dry_run=False,
                    )

        assert result == "moved"
        notion.update_status.assert_called_once()
        call_kwargs = notion.update_status.call_args[1]
        assert "Cool Channel" in call_kwargs["file_key"]

    def test_already_grouped_is_skipped(self, config):
        entry = _make_entry(file_key="MediaSync/harshit/audio/Channel/Song.m4a")
        notion = MagicMock()
        storage = MagicMock()

        result = _migrate_entry(
            entry, config, notion, storage,
            artwork_uploaded=set(), dry_run=False,
        )

        assert result == "skipped"
        notion.update_status.assert_not_called()

    def test_dry_run_does_not_move(self, config):
        entry = _make_entry(file_key="MediaSync/harshit/audio/Song.m4a")
        notion = MagicMock()
        storage = MagicMock()

        with patch("mediasync.migrate._get_channel_for_url", return_value="Channel"):
            result = _migrate_entry(
                entry, config, notion, storage,
                artwork_uploaded=set(), dry_run=True,
            )

        assert result == "moved"
        notion.update_status.assert_not_called()

    def test_no_channel_found_keeps_old_key(self, config):
        entry = _make_entry(file_key="MediaSync/harshit/audio/Song.m4a")
        notion = MagicMock()
        storage = MagicMock()

        with patch("mediasync.migrate._get_channel_for_url", return_value=None):
            result = _migrate_entry(
                entry, config, notion, storage,
                artwork_uploaded=set(), dry_run=False,
            )

        assert result == "skipped"

    def test_grouping_disabled_skips(self, config):
        config_no_group = Config(
            notion_token="tok",
            notion_database_id="db",
            profiles=[Profile("harshit")],
            storage_backend="s3",
            s3_bucket="bucket",
            group_by_channel=False,
            herald_enabled=False,
        )
        entry = _make_entry()
        result = _migrate_entry(
            entry, config_no_group, MagicMock(), MagicMock(),
            artwork_uploaded=set(), dry_run=False,
        )
        assert result == "skipped"


class TestGetChannelForUrl:
    def test_returns_channel_name(self):
        with patch("mediasync.migrate.get_metadata", return_value={"uploader": "My Channel"}):
            assert _get_channel_for_url("https://youtube.com/watch?v=x") == "My Channel"

    def test_returns_none_on_failure(self):
        from mediasync.downloader import DownloadError
        with patch("mediasync.migrate.get_metadata", side_effect=DownloadError("fail")):
            assert _get_channel_for_url("https://youtube.com/watch?v=x") is None

    def test_empty_uploader_returns_none(self):
        with patch("mediasync.migrate.get_metadata", return_value={"uploader": "", "channel": ""}):
            assert _get_channel_for_url("https://youtube.com/watch?v=x") is None


class TestSanitizeChannel:
    def test_removes_unsafe(self):
        assert _sanitize_channel('Artist: The "Best"') == 'Artist The Best'

    def test_truncates(self):
        assert len(_sanitize_channel("A" * 200)) == 100

    def test_empty_returns_unknown(self):
        assert _sanitize_channel("") == "Unknown"


class TestMigrate:
    def test_full_migration_flow(self, config):
        entry = _make_entry()
        notion = MagicMock()
        notion.get_done_for_profile.return_value = [entry]
        storage = MagicMock()

        with patch("mediasync.migrate.NotionClient", return_value=notion):
            with patch("mediasync.migrate.create_storage", return_value=storage):
                with patch("mediasync.migrate._migrate_entry", return_value="moved"):
                    with patch("mediasync.migrate.generate_standing_playlists", return_value=2):
                        stats = migrate(config)

        assert stats["moved"] == 1
        assert stats["playlists"] == 2

    def test_handles_errors_gracefully(self, config):
        entry = _make_entry()
        notion = MagicMock()
        notion.get_done_for_profile.return_value = [entry]
        storage = MagicMock()

        with patch("mediasync.migrate.NotionClient", return_value=notion):
            with patch("mediasync.migrate.create_storage", return_value=storage):
                with patch("mediasync.migrate._migrate_entry", side_effect=Exception("boom")):
                    with patch("mediasync.migrate.generate_standing_playlists", return_value=0):
                        stats = migrate(config)

        assert stats["failed"] == 1


class TestRegeneratePlaylists:
    def test_calls_generate_standing_playlists(self, config):
        with patch("mediasync.migrate.NotionClient") as MockNotion:
            with patch("mediasync.migrate.create_storage") as MockStorage:
                with patch("mediasync.migrate.generate_standing_playlists", return_value=4) as mock_gen:
                    count = regenerate_playlists(config)

        assert count == 4
        mock_gen.assert_called_once()
