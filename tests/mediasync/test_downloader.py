"""Tests for mediasync.downloader module."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

from mediasync.downloader import (
    DownloadError,
    DownloadResult,
    DurationExceededError,
    download,
    get_metadata,
    _build_cmd,
    _find_output,
    _sanitize_title,
)
from mediasync.notion_client import Format


class TestGetMetadata:
    def test_successful_metadata_fetch(self):
        meta = {"title": "Test", "duration": 300, "uploader": "TestChannel"}
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(meta)

        with patch("subprocess.run", return_value=result) as mock_run:
            data = get_metadata("https://youtube.com/watch?v=abc")

        assert data == meta
        cmd = mock_run.call_args[0][0]
        assert "yt-dlp" in cmd
        assert "--dump-json" in cmd
        assert "--no-playlist" in cmd

    def test_metadata_failure_raises(self):
        result = MagicMock()
        result.returncode = 1
        result.stderr = "ERROR: Video unavailable"

        with patch("subprocess.run", return_value=result):
            with pytest.raises(DownloadError, match="Metadata fetch failed"):
                get_metadata("https://youtube.com/watch?v=bad")

    def test_metadata_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("yt-dlp", 60)):
            with pytest.raises(subprocess.TimeoutExpired):
                get_metadata("https://youtube.com/watch?v=slow")


class TestDownload:
    @pytest.fixture
    def mock_metadata(self):
        return {
            "title": "Cool Song",
            "duration": 240,
            "uploader": "Artist",
            "channel": "ArtistChannel",
            "thumbnail": "https://img.youtube.com/thumb.jpg",
        }

    def test_audio_download(self, mock_metadata, tmp_path):
        output_dir = str(tmp_path)
        audio_file = tmp_path / "Cool Song.m4a"
        audio_file.write_bytes(b"fake m4a")

        success_result = MagicMock(returncode=0, stderr="")

        with patch("mediasync.downloader.get_metadata", return_value=mock_metadata):
            with patch("subprocess.run", return_value=success_result):
                results = download(
                    "https://youtube.com/watch?v=abc",
                    Format.AUDIO,
                    output_dir=output_dir,
                    max_duration_secs=7200,
                )

        assert len(results) == 1
        assert results[0].title == "Cool Song"
        assert results[0].artist == "Artist"
        assert results[0].duration_secs == 240
        assert results[0].format_type == "audio"

    def test_video_download(self, mock_metadata, tmp_path):
        output_dir = str(tmp_path)
        video_file = tmp_path / "Cool Song.mp4"
        video_file.write_bytes(b"fake mp4")

        success_result = MagicMock(returncode=0, stderr="")

        with patch("mediasync.downloader.get_metadata", return_value=mock_metadata):
            with patch("subprocess.run", return_value=success_result):
                results = download(
                    "https://youtube.com/watch?v=abc",
                    Format.VIDEO,
                    output_dir=output_dir,
                    max_duration_secs=7200,
                )

        assert len(results) == 1
        assert results[0].format_type == "video"

    def test_both_format_downloads_two(self, mock_metadata, tmp_path):
        output_dir = str(tmp_path)
        (tmp_path / "Cool Song.m4a").write_bytes(b"audio")
        (tmp_path / "Cool Song.mp4").write_bytes(b"video")

        success_result = MagicMock(returncode=0, stderr="")

        with patch("mediasync.downloader.get_metadata", return_value=mock_metadata):
            with patch("subprocess.run", return_value=success_result):
                results = download(
                    "https://youtube.com/watch?v=abc",
                    Format.BOTH,
                    output_dir=output_dir,
                    max_duration_secs=7200,
                )

        assert len(results) == 2
        types = {r.format_type for r in results}
        assert types == {"audio", "video"}

    def test_duration_exceeded_raises(self, tmp_path):
        meta = {"title": "Long", "duration": 8000, "uploader": "X", "thumbnail": ""}
        with patch("mediasync.downloader.get_metadata", return_value=meta):
            with pytest.raises(DurationExceededError, match="8000s exceeds limit"):
                download(
                    "https://youtube.com/watch?v=long",
                    Format.AUDIO,
                    output_dir=str(tmp_path),
                    max_duration_secs=7200,
                )

    def test_download_failure_raises(self, mock_metadata, tmp_path):
        fail_result = MagicMock(returncode=1, stderr="ERROR: something went wrong")

        with patch("mediasync.downloader.get_metadata", return_value=mock_metadata):
            with patch("subprocess.run", return_value=fail_result):
                with pytest.raises(DownloadError, match="yt-dlp failed"):
                    download(
                        "https://youtube.com/watch?v=fail",
                        Format.AUDIO,
                        output_dir=str(tmp_path),
                        max_duration_secs=7200,
                    )

    def test_missing_uploader_falls_back_to_channel(self, tmp_path):
        meta = {"title": "Song", "duration": 100, "channel": "FallbackCh", "thumbnail": ""}
        (tmp_path / "Song.m4a").write_bytes(b"data")
        success_result = MagicMock(returncode=0, stderr="")

        with patch("mediasync.downloader.get_metadata", return_value=meta):
            with patch("subprocess.run", return_value=success_result):
                results = download(
                    "https://youtube.com/watch?v=x",
                    Format.AUDIO,
                    output_dir=str(tmp_path),
                    max_duration_secs=7200,
                )

        assert results[0].artist == "FallbackCh"

    def test_no_uploader_or_channel_defaults_to_unknown(self, tmp_path):
        meta = {"title": "Song", "duration": 100, "thumbnail": ""}
        (tmp_path / "Song.m4a").write_bytes(b"data")
        success_result = MagicMock(returncode=0, stderr="")

        with patch("mediasync.downloader.get_metadata", return_value=meta):
            with patch("subprocess.run", return_value=success_result):
                results = download(
                    "https://youtube.com/watch?v=x",
                    Format.AUDIO,
                    output_dir=str(tmp_path),
                    max_duration_secs=7200,
                )

        assert results[0].artist == "Unknown"


class TestBuildCmd:
    def test_audio_cmd(self):
        cmd = _build_cmd("https://youtube.com/watch?v=x", "audio", "/tmp/out.m4a")
        assert "-x" in cmd
        assert "--audio-format" in cmd
        assert "m4a" in cmd
        assert "--no-playlist" in cmd

    def test_video_cmd(self):
        cmd = _build_cmd("https://youtube.com/watch?v=x", "video", "/tmp/out.mp4")
        assert "-f" in cmd
        assert "--merge-output-format" in cmd
        assert "mp4" in cmd
        assert "-x" not in cmd


class TestFindOutput:
    def test_finds_expected_file(self, tmp_path):
        expected = tmp_path / "song.m4a"
        expected.write_bytes(b"data")
        assert _find_output(expected) == expected

    def test_finds_alternative_extension(self, tmp_path):
        expected = tmp_path / "song.m4a"
        actual = tmp_path / "song.opus"
        actual.write_bytes(b"data")
        assert _find_output(expected) == actual

    def test_raises_if_not_found(self, tmp_path):
        expected = tmp_path / "nonexistent.m4a"
        with pytest.raises(DownloadError, match="Output file not found"):
            _find_output(expected)


class TestSanitizeTitle:
    def test_removes_unsafe_chars(self):
        assert _sanitize_title('Test: "Video" <1>') == "Test Video 1"

    def test_collapses_whitespace(self):
        assert _sanitize_title("too   many    spaces") == "too many spaces"

    def test_truncates_long_titles(self):
        long_title = "A" * 300
        assert len(_sanitize_title(long_title)) == 200

    def test_handles_empty_string(self):
        assert _sanitize_title("") == ""

    def test_removes_path_separators(self):
        assert _sanitize_title("path/to\\file") == "pathtofile"
