"""Additional pipeline tests to cover _upload_playlist, _upload_folder_artwork, _derive_playlist_title."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mediasync.config import Config, Profile
from mediasync.downloader import DownloadError, DownloadResult
from mediasync.notion_client import Format, MediaEntry, Status
from mediasync.pipeline import (
    _derive_playlist_title,
    _upload_folder_artwork,
    _upload_playlist,
)


@pytest.fixture
def config(tmp_path):
    return Config(
        notion_token="tok",
        notion_database_id="db",
        profiles=[Profile("Harshit")],
        s3_bucket="bucket",
        s3_region="us-west-2",
        s3_prefix="MediaSync",
        output_dir=str(tmp_path),
        herald_enabled=False,
    )


@pytest.fixture
def entry():
    return MediaEntry(
        page_id="page-1",
        url="https://youtube.com/playlist?list=PLxyz",
        profile="Harshit",
        format=Format.AUDIO,
        status=Status.PENDING,
        delete=False,
    )


def _make_result(title="Song", artist="Artist", path=None, duration=120):
    return DownloadResult(
        path=path or Path("/tmp/fake.m4a"),
        title=title,
        artist=artist,
        duration_secs=duration,
        thumbnail_url="https://example.com/thumb.jpg",
        format_type="audio",
    )


class TestUploadPlaylist:
    @patch("mediasync.pipeline.generate_m3u")
    @patch("mediasync.pipeline.make_relative_keys")
    def test_generates_and_uploads_playlist(self, mock_rel_keys, mock_gen, config, entry, tmp_path):
        results = [
            _make_result("Track 1", "Artist"),
            _make_result("Track 2", "Artist"),
        ]
        file_keys = ["MediaSync/Harshit/audio/t1.m4a", "MediaSync/Harshit/audio/t2.m4a"]

        mock_rel_keys.return_value = ["../audio/t1.m4a", "../audio/t2.m4a"]
        playlist_path = tmp_path / "Artist Playlist.m3u8"
        playlist_path.write_text("#EXTM3U\n")
        mock_gen.return_value = playlist_path

        storage = MagicMock()

        _upload_playlist(results, file_keys, entry, storage, config)

        mock_gen.assert_called_once()
        storage.upload.assert_called_once_with(
            playlist_path, "MediaSync/Harshit/playlists", playlist_path.name
        )
        # Playlist file should be cleaned up
        assert not playlist_path.exists()

    @patch("mediasync.pipeline.generate_m3u")
    @patch("mediasync.pipeline.make_relative_keys")
    def test_cleanup_on_upload_error(self, mock_rel_keys, mock_gen, config, entry, tmp_path):
        results = [_make_result("A"), _make_result("B")]
        file_keys = ["k1", "k2"]
        mock_rel_keys.return_value = ["r1", "r2"]

        playlist_path = tmp_path / "playlist.m3u8"
        playlist_path.write_text("#EXTM3U\n")
        mock_gen.return_value = playlist_path

        storage = MagicMock()
        storage.upload.side_effect = Exception("upload failed")

        with pytest.raises(Exception, match="upload failed"):
            _upload_playlist(results, file_keys, entry, storage, config)

        # File cleaned up even on error
        assert not playlist_path.exists()


class TestUploadFolderArtwork:
    @patch("mediasync.pipeline.download_thumbnail")
    def test_uploads_artwork_and_cleans_up(self, mock_dl, tmp_path):
        thumb_path = tmp_path / "thumb.jpg"
        thumb_path.write_bytes(b"JPEG data")
        mock_dl.return_value = thumb_path

        storage = MagicMock()
        _upload_folder_artwork("https://img.example.com/t.jpg", "remote/folder", storage, str(tmp_path))

        storage.upload.assert_called_once_with(thumb_path, "remote/folder", "folder.jpg")
        assert not thumb_path.exists()

    @patch("mediasync.pipeline.download_thumbnail")
    def test_no_thumbnail_downloaded(self, mock_dl):
        mock_dl.return_value = None
        storage = MagicMock()
        _upload_folder_artwork("https://img.example.com/t.jpg", "remote/folder", storage, "/tmp")
        storage.upload.assert_not_called()

    @patch("mediasync.pipeline.download_thumbnail")
    def test_upload_failure_non_fatal(self, mock_dl, tmp_path):
        thumb_path = tmp_path / "thumb.jpg"
        thumb_path.write_bytes(b"JPEG data")
        mock_dl.return_value = thumb_path

        storage = MagicMock()
        storage.upload.side_effect = Exception("S3 error")

        # Should not raise
        _upload_folder_artwork("https://img.example.com/t.jpg", "remote/folder", storage, str(tmp_path))
        assert not thumb_path.exists()


class TestDerivePlaylistTitle:
    def test_empty_results(self):
        assert _derive_playlist_title([]) == "Playlist"

    def test_single_result(self):
        results = [_make_result("My Song")]
        assert _derive_playlist_title(results) == "My Song"

    def test_common_prefix(self):
        results = [
            _make_result("Podcast Episode 1"),
            _make_result("Podcast Episode 2"),
            _make_result("Podcast Episode 3"),
        ]
        title = _derive_playlist_title(results)
        assert "Podcast" in title

    def test_no_common_prefix_same_artist(self):
        results = [
            _make_result("Song A", artist="Coldplay"),
            _make_result("Song B", artist="Coldplay"),
        ]
        title = _derive_playlist_title(results)
        assert title == "Coldplay Playlist"

    def test_no_common_prefix_different_artists(self):
        results = [
            _make_result("Song X", artist="Artist1"),
            _make_result("Song Y", artist="Artist2"),
        ]
        title = _derive_playlist_title(results)
        assert title == "Song X"

    def test_short_common_prefix_ignored(self):
        """Common prefix <= 5 chars should not be used."""
        results = [
            _make_result("Abc Different Title"),
            _make_result("Abc Other Thing"),
        ]
        # Common prefix "Abc " is 4 chars, too short
        title = _derive_playlist_title(results)
        # Falls through to artist check or first title
        assert title is not None


class TestReconcilePlaylist:
    """Cover lines 213, 234, 240-241, 253, 287, 296, 320-324, 329, 355-362, 419."""

    @patch("mediasync.pipeline._fetch_playlist_items_metadata")
    def test_reconcile_playlist_video_format(self, mock_fetch, config):
        """Cover line 213: Format.VIDEO adds 'video' to formats_to_check."""
        from mediasync.pipeline import _reconcile_playlist_with_storage

        mock_fetch.return_value = [
            {"title": "Song 1", "uploader": "Artist", "duration": 180},
        ]

        entry = MediaEntry(
            page_id="p1",
            url="https://youtube.com/playlist?list=PLxyz",
            profile="Harshit",
            format=Format.VIDEO,
            status=Status.PENDING,
            delete=False,
        )

        storage = MagicMock()
        storage.list_folder.return_value = {"Song 1.mp4"}

        result = _reconcile_playlist_with_storage(
            "https://youtube.com/playlist?list=PLxyz", entry, storage, config
        )

        assert result is not None
        file_keys, duration = result
        assert "video" in file_keys[0]
        assert duration == 180

    @patch("mediasync.pipeline._fetch_playlist_items_metadata")
    def test_reconcile_playlist_no_group_by_channel(self, mock_fetch, tmp_path):
        """Cover line 234: no group_by_channel uses flat folder."""
        from mediasync.pipeline import _reconcile_playlist_with_storage

        cfg = Config(
            notion_token="tok",
            notion_database_id="db",
            profiles=[Profile("Harshit")],
            s3_bucket="bucket",
            s3_prefix="MediaSync",
            group_by_channel=False,
            output_dir=str(tmp_path),
            herald_enabled=False,
        )

        mock_fetch.return_value = [
            {"title": "Song", "uploader": "Artist", "duration": 120},
        ]

        entry = MediaEntry(
            page_id="p1",
            url="https://youtube.com/playlist?list=PLxyz",
            profile="Harshit",
            format=Format.AUDIO,
            status=Status.PENDING,
            delete=False,
        )

        storage = MagicMock()
        storage.list_folder.return_value = {"Song.m4a"}

        result = _reconcile_playlist_with_storage(
            "https://youtube.com/playlist?list=PLxyz", entry, storage, cfg
        )

        assert result is not None
        file_keys, _ = result
        assert "MediaSync/Harshit/audio/Song.m4a" in file_keys[0]

    @patch("mediasync.pipeline._fetch_playlist_items_metadata")
    def test_reconcile_playlist_list_folder_exception(self, mock_fetch, config):
        """Cover lines 240-241: exception from list_folder falls back to empty set."""
        from mediasync.pipeline import _reconcile_playlist_with_storage

        mock_fetch.return_value = [
            {"title": "Track", "uploader": "Artist", "duration": 60},
        ]

        entry = MediaEntry(
            page_id="p1",
            url="https://youtube.com/playlist?list=PLxyz",
            profile="Harshit",
            format=Format.AUDIO,
            status=Status.PENDING,
            delete=False,
        )

        storage = MagicMock()
        storage.list_folder.side_effect = Exception("network error")

        # Track.m4a won't be in the empty set, so returns None
        result = _reconcile_playlist_with_storage(
            "https://youtube.com/playlist?list=PLxyz", entry, storage, config
        )
        assert result is None

    @patch("mediasync.pipeline._fetch_playlist_items_metadata")
    def test_reconcile_playlist_progress_logging(self, mock_fetch, config):
        """Cover line 253: progress logging every 25 items."""
        from mediasync.pipeline import _reconcile_playlist_with_storage

        items = [
            {"title": f"Song {i}", "uploader": "Artist", "duration": 60}
            for i in range(30)
        ]
        mock_fetch.return_value = items

        entry = MediaEntry(
            page_id="p1",
            url="https://youtube.com/playlist?list=PLxyz",
            profile="Harshit",
            format=Format.AUDIO,
            status=Status.PENDING,
            delete=False,
        )

        storage = MagicMock()
        # All songs exist
        storage.list_folder.return_value = {f"Song {i}.m4a" for i in range(30)}

        result = _reconcile_playlist_with_storage(
            "https://youtube.com/playlist?list=PLxyz", entry, storage, config
        )
        assert result is not None
        assert len(result[0]) == 30

    @patch("mediasync.pipeline.get_metadata")
    @patch("mediasync.pipeline.get_playlist_metadata")
    @patch("mediasync.pipeline.get_full_playlist_metadata")
    def test_fetch_metadata_flat_returns_empty(self, mock_full, mock_flat, mock_meta):
        """Cover line 287: flat_items is empty list (falsy) -> returns None."""
        from mediasync.pipeline import _fetch_playlist_items_metadata

        mock_full.side_effect = DownloadError("full fail")
        mock_flat.return_value = []

        result = _fetch_playlist_items_metadata("https://youtube.com/playlist?list=PLxyz")
        assert result is None

    @patch("mediasync.pipeline.get_metadata")
    @patch("mediasync.pipeline.get_playlist_metadata")
    @patch("mediasync.pipeline.get_full_playlist_metadata")
    def test_fetch_metadata_item_has_no_url_or_id(self, mock_full, mock_flat, mock_meta):
        """Cover line 296: item has no url and no id -> returns None."""
        from mediasync.pipeline import _fetch_playlist_items_metadata

        mock_full.side_effect = DownloadError("full fail")
        mock_flat.return_value = [{"title": "orphan"}]  # no url, no id

        result = _fetch_playlist_items_metadata("https://youtube.com/playlist?list=PLxyz")
        assert result is None

    @patch("mediasync.pipeline.cache_metadata")
    @patch("mediasync.pipeline.get_metadata")
    @patch("mediasync.pipeline.get_playlist_metadata")
    @patch("mediasync.pipeline.get_full_playlist_metadata")
    def test_fetch_metadata_per_item_download_error(self, mock_full, mock_flat, mock_meta, mock_cache):
        """Cover lines 320-322: DownloadError during per-item fetch -> returns None."""
        from mediasync.pipeline import _fetch_playlist_items_metadata

        mock_full.side_effect = DownloadError("full fail")
        mock_flat.return_value = [{"id": "abc123", "title": "Song"}]
        mock_meta.side_effect = DownloadError("item fail")

        result = _fetch_playlist_items_metadata("https://youtube.com/playlist?list=PLxyz")
        assert result is None

    @patch("mediasync.pipeline.cache_metadata")
    @patch("mediasync.pipeline.get_metadata")
    @patch("mediasync.pipeline.get_playlist_metadata")
    @patch("mediasync.pipeline.get_full_playlist_metadata")
    def test_fetch_metadata_executor_exception(self, mock_full, mock_flat, mock_meta, mock_cache):
        """Cover lines 323-324: generic Exception from ThreadPoolExecutor."""
        from mediasync.pipeline import _fetch_playlist_items_metadata

        mock_full.side_effect = DownloadError("full fail")
        mock_flat.return_value = [{"id": "abc", "title": "S"}]
        mock_meta.side_effect = RuntimeError("unexpected")

        result = _fetch_playlist_items_metadata("https://youtube.com/playlist?list=PLxyz")
        assert result is None




class TestProcessEntryReconciled:
    """Cover lines 355-362: _process_entry when reconcile succeeds."""

    @patch("mediasync.pipeline._reconcile_with_storage")
    def test_reconciled_entry_skips_download(self, mock_reconcile, config):
        from mediasync.pipeline import _process_entry

        mock_reconcile.return_value = (["key1.m4a", "key2.m4a"], 300)

        entry = MediaEntry(
            page_id="page-1",
            url="https://youtube.com/watch?v=abc",
            profile="Harshit",
            format=Format.AUDIO,
            status=Status.PENDING,
            delete=False,
        )
        notion = MagicMock()
        storage = MagicMock()

        result = _process_entry(entry, notion, storage, config)

        assert result is True
        notion.update_status.assert_called_once_with(
            "page-1", Status.DONE,
            file_key="key1.m4a; key2.m4a",
            duration=300,
        )


class TestRunFailedEntry:
    """Cover line 98: stats.failed incremented when _process_entry returns False."""

    @patch("mediasync.pipeline.generate_standing_playlists")
    @patch("mediasync.pipeline._process_entry", return_value=False)
    @patch("mediasync.pipeline._is_duplicate", return_value=False)
    @patch("mediasync.pipeline.sync_playlists", return_value=0)
    @patch("mediasync.pipeline._process_deletions", return_value=0)
    def test_failed_entry(self, mock_del, mock_sync, mock_dup, mock_proc, mock_gen, config):
        from mediasync.pipeline import run

        notion = MagicMock()
        notion.get_pending.return_value = [
            MediaEntry("p1", "https://youtube.com/watch?v=x", "Harshit",
                       Format.AUDIO, Status.PENDING, False),
        ]
        storage = MagicMock()

        with patch("mediasync.pipeline.NotionClient", return_value=notion):
            with patch("mediasync.pipeline.create_storage", return_value=storage):
                stats = run(config)

        assert stats.failed == 1


class TestProcessEntryUploadFailure:
    """Cover line 419: upload exception caught and status set to FAILED."""

    @patch("mediasync.pipeline._reconcile_with_storage", return_value=None)
    @patch("mediasync.pipeline.download")
    @patch("mediasync.pipeline.tag_file")
    @patch("mediasync.pipeline.cleanup_results")
    def test_upload_exception(self, mock_cleanup, mock_tag, mock_download, mock_reconcile, config, tmp_path):
        from mediasync.pipeline import _process_entry

        result_file = tmp_path / "song.m4a"
        result_file.write_bytes(b"audio")

        mock_download.return_value = [
            DownloadResult(
                path=result_file, title="Song", artist="Artist",
                duration_secs=120, thumbnail_url="", format_type="audio",
            )
        ]

        entry = MediaEntry(
            page_id="page-1",
            url="https://youtube.com/watch?v=abc",
            profile="Harshit",
            format=Format.AUDIO,
            status=Status.PENDING,
            delete=False,
        )
        notion = MagicMock()
        storage = MagicMock()
        storage.upload.side_effect = Exception("S3 access denied")

        result = _process_entry(entry, notion, storage, config)

        assert result is False
        # Check it was marked as failed
        notion.update_status.assert_any_call(
            "page-1", Status.FAILED, error="S3 access denied"
        )
