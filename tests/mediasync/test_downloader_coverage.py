"""Additional downloader tests to cover missing lines."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mediasync.downloader import (
    DownloadError,
    _find_output,
    _is_leftover_for_stem,
    get_full_playlist_metadata,
    get_metadata,
    get_playlist_metadata,
)


class TestGetMetadataCache:
    """Cover line 81: metadata cache hit."""

    @patch("mediasync.downloader._metadata_cache", {"https://cached.url": {"title": "cached"}})
    def test_cache_hit(self):
        result = get_metadata("https://cached.url")
        assert result == {"title": "cached"}


class TestGetPlaylistMetadata:
    @patch("mediasync.downloader._cookies_path", return_value=None)
    @patch("subprocess.run")
    def test_successful_fetch(self, mock_run, mock_cookies):
        entries = [{"id": "a", "title": "Song A"}, {"id": "b", "title": "Song B"}]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="\n".join(json.dumps(e) for e in entries),
            stderr="",
        )

        result = get_playlist_metadata("https://youtube.com/playlist?list=PL123")
        assert len(result) == 2
        assert result[0]["title"] == "Song A"

    @patch("mediasync.downloader._cookies_path", return_value=Path("/cookies.txt"))
    @patch("subprocess.run")
    def test_with_cookies(self, mock_run, mock_cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"id": "x"}),
            stderr="",
        )
        get_playlist_metadata("https://youtube.com/playlist?list=PL123")
        cmd = mock_run.call_args[0][0]
        assert "--cookies" in cmd

    @patch("mediasync.downloader._cookies_path", return_value=None)
    @patch("subprocess.run")
    def test_failure_raises(self, mock_run, mock_cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="ERROR: not found"
        )
        with pytest.raises(DownloadError, match="Playlist metadata fetch failed"):
            get_playlist_metadata("https://youtube.com/playlist?list=PL123")

    @patch("mediasync.downloader._cookies_path", return_value=None)
    @patch("subprocess.run")
    def test_empty_output_raises(self, mock_run, mock_cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with pytest.raises(DownloadError, match="empty or unavailable"):
            get_playlist_metadata("https://youtube.com/playlist?list=PL123")


class TestGetFullPlaylistMetadata:
    @patch("mediasync.downloader._cookies_path", return_value=None)
    @patch("subprocess.run")
    def test_successful_fetch(self, mock_run, mock_cookies):
        entries = [
            {"id": "a", "title": "Song A", "uploader": "Artist"},
            {"id": "b", "title": "Song B", "uploader": "Artist"},
        ]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="\n".join(json.dumps(e) for e in entries),
            stderr="",
        )
        result = get_full_playlist_metadata("https://youtube.com/playlist?list=PL123")
        assert len(result) == 2

    @patch("mediasync.downloader._cookies_path", return_value=None)
    @patch("subprocess.run")
    def test_failure_raises(self, mock_run, mock_cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="ERROR"
        )
        with pytest.raises(DownloadError, match="Full playlist metadata"):
            get_full_playlist_metadata("https://youtube.com/playlist?list=PL123")

    @patch("mediasync.downloader._cookies_path", return_value=None)
    @patch("subprocess.run")
    def test_empty_raises(self, mock_run, mock_cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with pytest.raises(DownloadError, match="empty or unavailable"):
            get_full_playlist_metadata("https://youtube.com/playlist?list=PL123")

    @patch("mediasync.downloader._cookies_path", return_value=None)
    @patch("subprocess.run")
    def test_bad_json_skipped(self, mock_run, mock_cookies):
        """Cover line that catches JSONDecodeError."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"id":"a"}\nnot-json\n{"id":"b"}',
            stderr="",
        )
        result = get_full_playlist_metadata("https://youtube.com/playlist?list=PL123")
        assert len(result) == 2


class TestIsLeftoverForStem:
    """Cover line 264: empty remainder returns False."""

    def test_empty_remainder(self):
        # name = "Song." matches prefix but remainder is empty
        assert _is_leftover_for_stem("Song.", "Song") is False


class TestFindOutput:
    """Cover lines 485-500."""

    def test_expected_exists(self, tmp_path):
        f = tmp_path / "song.m4a"
        f.write_bytes(b"audio")
        assert _find_output(f) == f

    def test_fallback_to_alt_extension(self, tmp_path):
        expected = tmp_path / "song.m4a"
        alt = tmp_path / "song.mp3"
        alt.write_bytes(b"audio")
        assert _find_output(expected) == alt

    def test_prefers_exact_stem_match(self, tmp_path):
        expected = tmp_path / "song.m4a"
        # This is `song.mp4` with same stem
        exact = tmp_path / "song.mp4"
        exact.write_bytes(b"video")
        # This is `song.f140.m4a` with format suffix
        leftover = tmp_path / "song.f140.m4a"
        leftover.write_bytes(b"raw")
        assert _find_output(expected) == exact

    def test_not_found_raises(self, tmp_path):
        expected = tmp_path / "missing.m4a"
        with pytest.raises(DownloadError, match="Output file not found"):
            _find_output(expected)

    def test_fallback_format_id_file(self, tmp_path):
        expected = tmp_path / "track.m4a"
        fallback = tmp_path / "track.f140.m4a"
        fallback.write_bytes(b"data")
        result = _find_output(expected)
        assert result == fallback
