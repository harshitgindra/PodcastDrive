"""Tests for mediasync.pipeline module."""

from unittest.mock import MagicMock, patch

import pytest

from mediasync.config import Config, Profile
from mediasync.downloader import DownloadError, DownloadResult, DurationExceededError
from mediasync.notion_client import Format, MediaEntry, Status
from mediasync.pipeline import (
    RunStats,
    _is_duplicate,
    _process_deletions,
    _process_entry,
    run,
)
from mediasync.storage import StorageError


@pytest.fixture
def config():
    return Config(
        notion_token="token",
        notion_database_id="db-id",
        s3_bucket="hg-mediafiles",
        s3_region="us-west-2",
        s3_prefix="MediaSync",
        profiles=[Profile("Harshit"), Profile("Dishita")],
        max_duration_secs=7200,
        output_dir="/tmp/mediasync",
        herald_enabled=False,
        herald_job_id="mediasync",
    )


@pytest.fixture
def pending_entry():
    return MediaEntry(
        page_id="page-1",
        url="https://youtube.com/watch?v=abc",
        profile="Harshit",
        format=Format.AUDIO,
        status=Status.PENDING,
        delete=False,
    )


@pytest.fixture
def done_entry():
    return MediaEntry(
        page_id="page-2",
        url="https://youtube.com/watch?v=abc",
        profile="Harshit",
        format=Format.AUDIO,
        status=Status.DONE,
        delete=False,
        file_key="MediaSync/Harshit/audio/song.m4a",
    )


@pytest.fixture
def delete_entry():
    return MediaEntry(
        page_id="page-3",
        url="https://youtube.com/watch?v=xyz",
        profile="Harshit",
        format=Format.AUDIO,
        status=Status.DONE,
        delete=True,
        file_key="MediaSync/Harshit/audio/old.m4a",
    )


class TestRun:
    @patch("mediasync.pipeline.create_storage")
    @patch("mediasync.pipeline.NotionClient")
    def test_empty_run(self, MockNotion, MockStorage, config):
        mock_notion = MockNotion.return_value
        mock_notion.get_deletions.return_value = []
        mock_notion.get_pending.return_value = []

        stats = run(config)

        assert stats == RunStats(processed=0, failed=0, deleted=0, skipped=0)

    @patch("mediasync.pipeline._process_entry", return_value=True)
    @patch("mediasync.pipeline._is_duplicate", return_value=False)
    @patch("mediasync.pipeline.create_storage")
    @patch("mediasync.pipeline.NotionClient")
    def test_processes_pending(self, MockNotion, MockStorage, mock_dup, mock_proc, config, pending_entry):
        mock_notion = MockNotion.return_value
        mock_notion.get_deletions.return_value = []
        mock_notion.get_pending.return_value = [pending_entry]

        stats = run(config)

        assert stats.processed == 1
        mock_proc.assert_called_once()

    @patch("mediasync.pipeline._is_duplicate", return_value=True)
    @patch("mediasync.pipeline.create_storage")
    @patch("mediasync.pipeline.NotionClient")
    def test_skips_duplicates(self, MockNotion, MockStorage, mock_dup, config, pending_entry):
        mock_notion = MockNotion.return_value
        mock_notion.get_deletions.return_value = []
        mock_notion.get_pending.return_value = [pending_entry]

        stats = run(config)

        assert stats.skipped == 1
        mock_notion.update_status.assert_called_once()

    @patch("mediasync.pipeline._is_duplicate", return_value=False)
    @patch("mediasync.pipeline.create_storage")
    @patch("mediasync.pipeline.NotionClient")
    def test_skips_unknown_profile(self, MockNotion, MockStorage, mock_dup, config, pending_entry):
        pending_entry.profile = "unknown_person"
        mock_notion = MockNotion.return_value
        mock_notion.get_deletions.return_value = []
        mock_notion.get_pending.return_value = [pending_entry]

        stats = run(config)

        assert stats.skipped == 1


class TestProcessDeletions:
    def test_deletes_and_archives(self, delete_entry):
        mock_notion = MagicMock()
        mock_notion.get_deletions.return_value = [delete_entry]
        mock_storage = MagicMock()

        count = _process_deletions(mock_notion, mock_storage)

        assert count == 1
        mock_storage.delete_file.assert_called_once_with("MediaSync/Harshit/audio/old.m4a")
        mock_notion.archive_page.assert_called_once_with("page-3")

    def test_handles_multiple_file_keys(self):
        entry = MediaEntry(
            page_id="p1", url="u", profile="h", format=Format.BOTH,
            status=Status.DONE, delete=True,
            file_key="MediaSync/h/audio/s.m4a; MediaSync/h/video/s.mp4",
        )
        mock_notion = MagicMock()
        mock_notion.get_deletions.return_value = [entry]
        mock_storage = MagicMock()

        _process_deletions(mock_notion, mock_storage)

        assert mock_storage.delete_file.call_count == 2

    def test_storage_error_does_not_crash(self, delete_entry):
        mock_notion = MagicMock()
        mock_notion.get_deletions.return_value = [delete_entry]
        mock_storage = MagicMock()
        mock_storage.delete_file.side_effect = StorageError("access denied")

        count = _process_deletions(mock_notion, mock_storage)

        assert count == 0
        mock_notion.archive_page.assert_not_called()

    def test_empty_file_key_still_archives(self):
        entry = MediaEntry(
            page_id="p1", url="u", profile="h", format=Format.AUDIO,
            status=Status.DONE, delete=True, file_key="",
        )
        mock_notion = MagicMock()
        mock_notion.get_deletions.return_value = [entry]
        mock_storage = MagicMock()

        count = _process_deletions(mock_notion, mock_storage)

        assert count == 1
        mock_storage.delete_file.assert_not_called()
        mock_notion.archive_page.assert_called_once()


class TestIsDuplicate:
    def test_duplicate_detected(self, pending_entry, done_entry):
        mock_notion = MagicMock()
        mock_notion.get_done_for_profile.return_value = [done_entry]

        assert _is_duplicate(mock_notion, pending_entry) is True

    def test_not_duplicate(self, pending_entry):
        mock_notion = MagicMock()
        mock_notion.get_done_for_profile.return_value = []

        assert _is_duplicate(mock_notion, pending_entry) is False

    def test_different_url_not_duplicate(self, pending_entry, done_entry):
        done_entry.url = "https://youtube.com/watch?v=different"
        mock_notion = MagicMock()
        mock_notion.get_done_for_profile.return_value = [done_entry]

        assert _is_duplicate(mock_notion, pending_entry) is False


class TestProcessEntry:
    def test_successful_audio_download(self, pending_entry, config, tmp_path):
        audio_path = tmp_path / "song.m4a"
        audio_path.write_bytes(b"audio data")

        mock_notion = MagicMock()
        mock_storage = MagicMock()
        mock_storage.upload.return_value = "MediaSync/Harshit/audio/song.m4a"

        dl_result = DownloadResult(
            path=audio_path,
            title="Song",
            artist="Artist",
            duration_secs=180,
            thumbnail_url="https://img/thumb.jpg",
            format_type="audio",
        )

        with patch("mediasync.pipeline.download", return_value=[dl_result]):
            with patch("mediasync.pipeline.tag_file"):
                success = _process_entry(pending_entry, mock_notion, mock_storage, config)

        assert success is True
        mock_notion.update_status.assert_any_call(pending_entry.page_id, Status.DOWNLOADING)
        final_call = mock_notion.update_status.call_args_list[-1]
        assert final_call[0][1] == Status.DONE

    def test_duration_exceeded(self, pending_entry, config):
        mock_notion = MagicMock()
        mock_storage = MagicMock()

        with patch("mediasync.pipeline.download", side_effect=DurationExceededError("too long")):
            success = _process_entry(pending_entry, mock_notion, mock_storage, config)

        assert success is False
        mock_notion.update_status.assert_any_call(
            pending_entry.page_id, Status.FAILED, error="too long"
        )

    def test_download_error(self, pending_entry, config):
        mock_notion = MagicMock()
        mock_storage = MagicMock()

        with patch("mediasync.pipeline.download", side_effect=DownloadError("network fail")):
            success = _process_entry(pending_entry, mock_notion, mock_storage, config)

        assert success is False

    def test_upload_error(self, pending_entry, config, tmp_path):
        audio_path = tmp_path / "song.m4a"
        audio_path.write_bytes(b"data")

        mock_notion = MagicMock()
        mock_storage = MagicMock()
        mock_storage.upload.side_effect = StorageError("access denied")

        dl_result = DownloadResult(
            path=audio_path, title="Song", artist="A",
            duration_secs=100, thumbnail_url="", format_type="audio",
        )

        with patch("mediasync.pipeline.download", return_value=[dl_result]):
            with patch("mediasync.pipeline.tag_file"):
                success = _process_entry(pending_entry, mock_notion, mock_storage, config)

        assert success is False
        assert not audio_path.exists()

    def test_both_format_uploads_two_files(self, config, tmp_path):
        entry = MediaEntry(
            page_id="p1", url="https://youtube.com/watch?v=x",
            profile="Harshit", format=Format.BOTH, status=Status.PENDING, delete=False,
        )
        audio_path = tmp_path / "song.m4a"
        video_path = tmp_path / "song.mp4"
        audio_path.write_bytes(b"audio")
        video_path.write_bytes(b"video")

        mock_notion = MagicMock()
        mock_storage = MagicMock()
        mock_storage.upload.side_effect = [
            "MediaSync/Harshit/audio/song.m4a",
            "MediaSync/Harshit/video/song.mp4",
        ]

        results = [
            DownloadResult(path=audio_path, title="Song", artist="A", duration_secs=100, thumbnail_url="", format_type="audio"),
            DownloadResult(path=video_path, title="Song", artist="A", duration_secs=100, thumbnail_url="", format_type="video"),
        ]

        with patch("mediasync.pipeline.download", return_value=results):
            with patch("mediasync.pipeline.tag_file"):
                success = _process_entry(entry, mock_notion, mock_storage, config)

        assert success is True
        assert mock_storage.upload.call_count == 2
        final_call = mock_notion.update_status.call_args_list[-1]
        file_key = final_call[1]["file_key"]
        assert "audio" in file_key and "video" in file_key

    def test_temp_files_cleaned_on_success(self, pending_entry, config, tmp_path):
        audio_path = tmp_path / "song.m4a"
        audio_path.write_bytes(b"data")

        mock_notion = MagicMock()
        mock_storage = MagicMock()
        mock_storage.upload.return_value = "key"

        dl_result = DownloadResult(
            path=audio_path, title="S", artist="A",
            duration_secs=60, thumbnail_url="", format_type="audio",
        )

        with patch("mediasync.pipeline.download", return_value=[dl_result]):
            with patch("mediasync.pipeline.tag_file"):
                _process_entry(pending_entry, mock_notion, mock_storage, config)

        assert not audio_path.exists()


class TestReset:
    @patch("mediasync.notion_client.NotionClient.get_processed")
    @patch("mediasync.notion_client.NotionClient.reset_status")
    @patch("mediasync.notion_client.NotionClient.__init__", return_value=None)
    def test_reset_clears_processed(self, mock_init, mock_reset, mock_get, config):
        from mediasync.cli import _reset

        mock_get.return_value = [
            MediaEntry(page_id="p1", url="u1", profile="Harshit", format=Format.AUDIO, status=Status.DONE, delete=False),
            MediaEntry(page_id="p2", url="u2", profile="Harshit", format=Format.AUDIO, status=Status.FAILED, delete=False),
        ]

        _reset(config)

        assert mock_reset.call_count == 2
        mock_reset.assert_any_call("p1")
        mock_reset.assert_any_call("p2")

    @patch("mediasync.notion_client.NotionClient.get_processed")
    @patch("mediasync.notion_client.NotionClient.reset_status")
    @patch("mediasync.notion_client.NotionClient.__init__", return_value=None)
    def test_reset_nothing_to_reset(self, mock_init, mock_reset, mock_get, config):
        from mediasync.cli import _reset

        mock_get.return_value = []

        _reset(config)  # Should not raise

        mock_reset.assert_not_called()


class TestProcessEntryCleanup:
    """Temp files are always removed and non-StorageError failures are caught (Fix #15)."""

    @staticmethod
    def _result(path):
        return DownloadResult(
            path=path, title="Song", artist="A", duration_secs=100, thumbnail_url="", format_type="audio"
        )

    def test_files_removed_after_successful_upload(self, pending_entry, config, tmp_path):
        audio = tmp_path / "song.m4a"
        audio.write_bytes(b"audio")
        storage = MagicMock()
        storage.upload.return_value = "key"

        with patch("mediasync.pipeline.download", return_value=[self._result(audio)]):
            with patch("mediasync.pipeline.tag_file"):
                assert _process_entry(pending_entry, MagicMock(), storage, config) is True

        assert not audio.exists()

    def test_files_removed_after_failed_upload(self, pending_entry, config, tmp_path):
        audio = tmp_path / "song.m4a"
        audio.write_bytes(b"audio")
        storage = MagicMock()
        storage.upload.side_effect = StorageError("access denied")

        with patch("mediasync.pipeline.download", return_value=[self._result(audio)]):
            with patch("mediasync.pipeline.tag_file"):
                assert _process_entry(pending_entry, MagicMock(), storage, config) is False

        assert not audio.exists()

    def test_tagging_error_is_caught_and_files_removed(self, pending_entry, config, tmp_path):
        """tag_file raises mutagen errors, not StorageError — those must not escape."""
        audio = tmp_path / "song.m4a"
        audio.write_bytes(b"audio")
        notion = MagicMock()

        with patch("mediasync.pipeline.download", return_value=[self._result(audio)]):
            with patch("mediasync.pipeline.tag_file", side_effect=OSError("corrupt tags")):
                assert _process_entry(pending_entry, notion, MagicMock(), config) is False

        assert not audio.exists()
        assert notion.update_status.call_args_list[-1][0][1] == Status.FAILED

    def test_missing_temp_file_does_not_raise(self, pending_entry, config, tmp_path):
        storage = MagicMock()
        storage.upload.return_value = "key"
        ghost = tmp_path / "gone.m4a"

        with patch("mediasync.pipeline.download", return_value=[self._result(ghost)]):
            with patch("mediasync.pipeline.tag_file"):
                assert _process_entry(pending_entry, MagicMock(), storage, config) is True


class TestProcessDeletionsErrorHandling:
    def test_non_storage_error_is_caught(self, delete_entry, config):
        """The old `except (StorageError, Exception)` tuple was a no-op alias for Exception."""
        notion = MagicMock()
        notion.get_deletions.return_value = [delete_entry]
        storage = MagicMock()
        storage.delete_file.side_effect = RuntimeError("unexpected")

        assert _process_deletions(notion, storage) == 0
        notion.archive_page.assert_not_called()
