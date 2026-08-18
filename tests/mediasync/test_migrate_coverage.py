"""Additional migrate tests to cover missing lines."""

from unittest.mock import MagicMock, patch

import pytest

from mediasync.config import Config, Profile
from mediasync.migrate import (
    _migrate_entry,
    _move_file,
    _onedrive_move,
    _upload_artwork_for_entry,
    migrate,
)
from mediasync.notion_client import Format, MediaEntry, Status


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


class TestMigrateEntryNoFileKey:
    """Cover line 58-59: entry with no file_key gets skipped."""

    def test_no_file_key_skipped(self, config):
        entry = _make_entry(file_key="")
        notion = MagicMock()
        notion.get_done_for_profile.return_value = [entry]

        with patch("mediasync.migrate.NotionClient", return_value=notion):
            with patch("mediasync.migrate.create_storage", return_value=MagicMock()):
                with patch("mediasync.migrate.generate_standing_playlists", return_value=0):
                    stats = migrate(config)

        assert stats["skipped"] == 1


class TestMigrateEntrySkippedResult:
    """Cover line 69-70: _migrate_entry returns 'skipped'."""

    def test_migrate_entry_returns_skipped(self, config):
        entry = _make_entry(file_key="MediaSync/harshit/audio/Song.m4a")
        notion = MagicMock()
        notion.get_done_for_profile.return_value = [entry]

        with patch("mediasync.migrate.NotionClient", return_value=notion):
            with patch("mediasync.migrate.create_storage", return_value=MagicMock()):
                with patch("mediasync.migrate._migrate_entry", return_value="skipped"):
                    with patch("mediasync.migrate.generate_standing_playlists", return_value=0):
                        stats = migrate(config)

        assert stats["skipped"] == 1


class TestMoveFileFailed:
    """Cover line 142: _move_file returns False, key kept."""

    def test_move_file_failure_keeps_old_key(self, config):
        entry = _make_entry(file_key="MediaSync/harshit/audio/Song.m4a")
        notion = MagicMock()
        storage = MagicMock()

        with patch("mediasync.migrate._get_channel_for_url", return_value="Channel"):
            with patch("mediasync.migrate._move_file", return_value=False):
                result = _migrate_entry(
                    entry, config, notion, storage,
                    artwork_uploaded=set(), dry_run=False,
                )

        assert result == "skipped"
        notion.update_status.assert_not_called()


class TestMoveFileS3:
    """Cover lines 179-187: _move_file with non-OneDrive storage (S3)."""

    def test_s3_storage_returns_false(self):
        storage = MagicMock()  # Not an OneDriveClient instance
        result = _move_file(storage, "old/path.m4a", "new/path.m4a")
        assert result is False


class TestOneDriveMove:
    """Cover lines 192-227: _onedrive_move success and failure."""

    @patch("urllib.request.urlopen")
    def test_successful_move(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = MagicMock()
        client._access_token = "token123"
        result = _onedrive_move(client, "old/file.m4a", "new/folder/file.m4a")
        assert result is True

    @patch("urllib.request.urlopen")
    def test_http_error_401_retries(self, mock_urlopen):
        import urllib.error

        # First call: 401, second call: success
        http_err = urllib.error.HTTPError(
            "url", 401, "Unauthorized", {}, None
        )
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [http_err, mock_resp]

        client = MagicMock()
        client._access_token = "old_token"
        client._refresh_access_token.return_value = "new_token"

        result = _onedrive_move(client, "old/file.m4a", "new/folder/file.m4a")
        assert result is True
        client._refresh_access_token.assert_called_once()

    @patch("urllib.request.urlopen")
    def test_http_error_500_returns_false(self, mock_urlopen):
        import urllib.error

        http_err = urllib.error.HTTPError(
            "url", 500, "Server Error", {}, None
        )
        mock_urlopen.side_effect = http_err

        client = MagicMock()
        client._access_token = "token"
        result = _onedrive_move(client, "old/file.m4a", "new/folder/file.m4a")
        assert result is False

    @patch("urllib.request.urlopen")
    def test_generic_exception_returns_false(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("network down")

        client = MagicMock()
        client._access_token = "token"
        result = _onedrive_move(client, "old/file.m4a", "new/folder/file.m4a")
        assert result is False


class TestUploadArtworkForEntry:
    """Cover lines 234-245."""

    @patch("mediasync.migrate.download_thumbnail")
    @patch("mediasync.migrate.get_metadata")
    def test_uploads_artwork(self, mock_meta, mock_thumb, tmp_path):
        mock_meta.return_value = {"thumbnail": "https://example.com/thumb.jpg"}
        thumb_file = tmp_path / "thumb.jpg"
        thumb_file.write_bytes(b"fake image")
        mock_thumb.return_value = thumb_file

        storage = MagicMock()
        _upload_artwork_for_entry("https://youtube.com/watch?v=x", "remote/folder", storage, str(tmp_path))

        storage.upload.assert_called_once_with(thumb_file, "remote/folder", "folder.jpg")

    @patch("mediasync.migrate.get_metadata")
    def test_exception_is_non_fatal(self, mock_meta):
        mock_meta.side_effect = Exception("network error")
        storage = MagicMock()
        # Should not raise
        _upload_artwork_for_entry("https://youtube.com/watch?v=x", "remote/folder", storage, "/tmp")

    @patch("mediasync.migrate.download_thumbnail")
    @patch("mediasync.migrate.get_metadata")
    def test_no_thumbnail_in_meta(self, mock_meta, mock_thumb):
        mock_meta.return_value = {"thumbnail": ""}
        storage = MagicMock()
        _upload_artwork_for_entry("https://youtube.com/watch?v=x", "remote/folder", storage, "/tmp")
        mock_thumb.assert_not_called()
        storage.upload.assert_not_called()
