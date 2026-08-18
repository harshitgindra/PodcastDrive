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


@pytest.fixture(autouse=True)
def _skip_reconciliation():
    """Skip storage reconciliation in pipeline tests (tested separately)."""
    with patch("mediasync.pipeline._reconcile_with_storage", return_value=None):
        yield


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

    def test_missing_temp_file_is_reported_not_silently_uploaded(self, pending_entry, config, tmp_path):
        """A file that vanished between download and upload used to sail through
        (mutagen and the storage backend were both happy to be handed a ghost path,
        so the row was marked done and nothing was actually stored).  It must now
        fail loudly, naming the path."""
        storage = MagicMock()
        storage.upload.return_value = "key"
        ghost = tmp_path / "gone.m4a"
        notion = MagicMock()

        with patch("mediasync.pipeline.download", return_value=[self._result(ghost)]):
            with patch("mediasync.pipeline.tag_file"):
                assert _process_entry(pending_entry, notion, storage, config) is False

        storage.upload.assert_not_called()
        last = notion.update_status.call_args_list[-1]
        assert last[0][1] == Status.FAILED
        assert "gone.m4a" in last[1]["error"]


class TestProcessEntryPartialFailure:
    """One bad item in a playlist used to discard the whole entry: the exception
    escaped the upload loop, the row was marked failed with a bare exception
    string, and the items that had already uploaded were never recorded anywhere.
    """

    @staticmethod
    def _result(path, title):
        return DownloadResult(
            path=path, title=title, artist="A", duration_secs=100, thumbnail_url="", format_type="audio"
        )

    def _three_items(self, tmp_path):
        results = []
        for name in ("one", "two", "three"):
            f = tmp_path / f"{name}.m4a"
            f.write_bytes(b"audio")
            results.append(self._result(f, name))
        return results

    def test_successful_items_are_still_uploaded_and_recorded(self, pending_entry, config, tmp_path):
        results = self._three_items(tmp_path)
        notion = MagicMock()
        storage = MagicMock()
        # Middle item fails; the other two must still be uploaded.
        storage.upload.side_effect = ["key-one", RuntimeError("network blip"), "key-three"]

        with patch("mediasync.pipeline.download", return_value=results):
            with patch("mediasync.pipeline.tag_file"):
                ok = _process_entry(pending_entry, notion, storage, config)

        assert ok is False  # the entry is not complete
        assert storage.upload.call_count == 3
        last = notion.update_status.call_args_list[-1]
        assert last[0][1] == Status.FAILED
        assert last[1]["file_key"] == "key-one; key-three"
        assert last[1]["duration"] == 200  # only the two that uploaded
        assert "uploaded 2 of 3" in last[1]["error"]
        assert "two: network blip" in last[1]["error"]

    def test_all_items_failing_is_reported_as_a_total_failure(self, pending_entry, config, tmp_path):
        results = self._three_items(tmp_path)
        notion = MagicMock()
        storage = MagicMock()
        storage.upload.side_effect = RuntimeError("bucket gone")

        with patch("mediasync.pipeline.download", return_value=results):
            with patch("mediasync.pipeline.tag_file"):
                assert _process_entry(pending_entry, notion, storage, config) is False

        last = notion.update_status.call_args_list[-1]
        assert last[0][1] == Status.FAILED
        assert "all 3 item(s) failed" in last[1]["error"]
        assert "file_key" not in last[1]

    def test_temp_files_are_cleaned_up_even_after_a_partial_failure(self, pending_entry, config, tmp_path):
        results = self._three_items(tmp_path)
        storage = MagicMock()
        storage.upload.side_effect = ["k1", RuntimeError("blip"), "k3"]

        with patch("mediasync.pipeline.download", return_value=results):
            with patch("mediasync.pipeline.tag_file"):
                _process_entry(pending_entry, MagicMock(), storage, config)

        assert [r.path.exists() for r in results] == [False, False, False]

    def test_playlist_m3u_only_references_items_that_uploaded(self, pending_entry, config, tmp_path):
        """An M3U built from all results would point at keys that were never stored."""
        results = self._three_items(tmp_path)
        storage = MagicMock()
        storage.upload.side_effect = ["key-one", RuntimeError("blip"), "key-three"]

        with (
            patch("mediasync.pipeline.download", return_value=results),
            patch("mediasync.pipeline.tag_file"),
            patch("mediasync.pipeline.is_playlist", return_value=True),
            patch("mediasync.pipeline._upload_playlist") as mock_playlist,
        ):
            _process_entry(pending_entry, MagicMock(), storage, config)

        passed_results, passed_keys = mock_playlist.call_args[0][0], mock_playlist.call_args[0][1]
        assert [r.title for r in passed_results] == ["one", "three"]
        assert passed_keys == ["key-one", "key-three"]

    def test_playlist_upload_failure_does_not_fail_the_entry(self, pending_entry, config, tmp_path):
        """The media is already stored; losing the convenience M3U must not undo that."""
        results = self._three_items(tmp_path)
        notion = MagicMock()
        storage = MagicMock()
        storage.upload.return_value = "key"

        with (
            patch("mediasync.pipeline.download", return_value=results),
            patch("mediasync.pipeline.tag_file"),
            patch("mediasync.pipeline.is_playlist", return_value=True),
            patch("mediasync.pipeline._upload_playlist", side_effect=RuntimeError("m3u boom")),
        ):
            assert _process_entry(pending_entry, notion, storage, config) is True

        assert notion.update_status.call_args_list[-1][0][1] == Status.DONE

    def test_vanished_file_logs_path_and_directory_contents(self, pending_entry, config, tmp_path, caplog):
        """The original mystery failure was a bare ENOENT with no context about which
        item it was or what else was in the directory."""
        present = tmp_path / "kept.m4a"
        present.write_bytes(b"audio")
        ghost = tmp_path / "vanished.m4a"
        storage = MagicMock()
        storage.upload.return_value = "key"

        results = [self._result(present, "kept"), self._result(ghost, "vanished")]
        with (
            caplog.at_level("ERROR", logger="mediasync.pipeline"),
            patch("mediasync.pipeline.download", return_value=results),
            patch("mediasync.pipeline.tag_file"),
        ):
            _process_entry(pending_entry, MagicMock(), storage, config)

        text = caplog.text
        assert str(ghost) in text
        assert "kept.m4a" in text  # directory listing
        assert "free=" in text


class TestProcessDeletionsErrorHandling:
    def test_non_storage_error_is_caught(self, delete_entry, config):
        """The old `except (StorageError, Exception)` tuple was a no-op alias for Exception."""
        notion = MagicMock()
        notion.get_deletions.return_value = [delete_entry]
        storage = MagicMock()
        storage.delete_file.side_effect = RuntimeError("unexpected")

        assert _process_deletions(notion, storage) == 0
        notion.archive_page.assert_not_called()


class TestChannelGrouping:
    def test_groups_by_channel_when_enabled(self, config, pending_entry, tmp_path):
        """When group_by_channel=True, upload path includes channel folder."""
        from mediasync.config import Config, Profile

        cfg = Config(
            notion_token="token",
            notion_database_id="db-id",
            s3_bucket="bucket",
            profiles=[Profile("Harshit")],
            max_duration_secs=7200,
            group_by_channel=True,
            output_dir=str(tmp_path),
            herald_enabled=False,
        )

        result = DownloadResult(
            path=tmp_path / "song.m4a",
            title="Song",
            artist="Cool Channel",
            duration_secs=120,
            thumbnail_url="",
            format_type="audio",
        )
        (tmp_path / "song.m4a").write_bytes(b"fake")

        notion = MagicMock()
        storage = MagicMock()
        storage.upload.return_value = "MediaSync/Harshit/audio/Cool Channel/song.m4a"

        with patch("mediasync.pipeline.download", return_value=[result]):
            with patch("mediasync.pipeline.tag_file"):
                success = _process_entry(pending_entry, notion, storage, cfg)

        assert success is True
        upload_call = storage.upload.call_args
        remote_folder = upload_call[0][1]
        assert "Cool Channel" in remote_folder

    def test_no_grouping_when_disabled(self, config, pending_entry, tmp_path):
        """When group_by_channel=False, upload path does not include channel."""
        from mediasync.config import Config, Profile

        cfg = Config(
            notion_token="token",
            notion_database_id="db-id",
            s3_bucket="bucket",
            profiles=[Profile("Harshit")],
            max_duration_secs=7200,
            group_by_channel=False,
            output_dir=str(tmp_path),
            herald_enabled=False,
        )

        result = DownloadResult(
            path=tmp_path / "song.m4a",
            title="Song",
            artist="Cool Channel",
            duration_secs=120,
            thumbnail_url="",
            format_type="audio",
        )
        (tmp_path / "song.m4a").write_bytes(b"fake")

        notion = MagicMock()
        storage = MagicMock()
        storage.upload.return_value = "MediaSync/Harshit/audio/song.m4a"

        with patch("mediasync.pipeline.download", return_value=[result]):
            with patch("mediasync.pipeline.tag_file"):
                success = _process_entry(pending_entry, notion, storage, cfg)

        assert success is True
        upload_call = storage.upload.call_args
        remote_folder = upload_call[0][1]
        assert "Cool Channel" not in remote_folder

    def test_unknown_artist_not_grouped(self, config, pending_entry, tmp_path):
        """When artist is 'Unknown', don't create a folder for it."""
        from mediasync.config import Config, Profile

        cfg = Config(
            notion_token="token",
            notion_database_id="db-id",
            s3_bucket="bucket",
            profiles=[Profile("Harshit")],
            max_duration_secs=7200,
            group_by_channel=True,
            output_dir=str(tmp_path),
            herald_enabled=False,
        )

        result = DownloadResult(
            path=tmp_path / "song.m4a",
            title="Song",
            artist="Unknown",
            duration_secs=120,
            thumbnail_url="",
            format_type="audio",
        )
        (tmp_path / "song.m4a").write_bytes(b"fake")

        notion = MagicMock()
        storage = MagicMock()
        storage.upload.return_value = "MediaSync/Harshit/audio/song.m4a"

        with patch("mediasync.pipeline.download", return_value=[result]):
            with patch("mediasync.pipeline.tag_file"):
                success = _process_entry(pending_entry, notion, storage, cfg)

        assert success is True
        upload_call = storage.upload.call_args
        remote_folder = upload_call[0][1]
        assert "Unknown" not in remote_folder


class TestSanitizeFolderName:
    from mediasync.pipeline import _sanitize_folder_name

    def test_removes_unsafe_chars(self):
        from mediasync.pipeline import _sanitize_folder_name
        assert _sanitize_folder_name('Artist: "The Best"') == 'Artist The Best'

    def test_truncates_long_names(self):
        from mediasync.pipeline import _sanitize_folder_name
        assert len(_sanitize_folder_name("A" * 200)) == 100

    def test_empty_returns_unknown(self):
        from mediasync.pipeline import _sanitize_folder_name
        assert _sanitize_folder_name("") == "Unknown"
        assert _sanitize_folder_name("???") == "Unknown"
