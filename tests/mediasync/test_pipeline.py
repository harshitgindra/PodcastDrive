"""Tests for mediasync.pipeline module."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from mediasync.config import Config, Profile
from mediasync.downloader import DownloadError, DownloadResult, DurationExceededError
from mediasync.notion_client import Format, MediaEntry, NotionClient, Status
from mediasync.storage import StorageError
from mediasync.pipeline import (
    RunStats,
    run,
    _is_duplicate,
    _process_deletions,
    _process_entry,
)


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
