"""Additional downloader tests to cover missing lines."""

import json
import subprocess
from unittest.mock import patch

import pytest

from mediasync.downloader import (
    DownloadError,
    _build_cmd,
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
    @patch("mediasync.downloader.cookie_args", return_value=[])
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

    @patch("mediasync.downloader.cookie_args", return_value=["--cookies", "/cookies.txt"])
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

    @patch("mediasync.downloader.cookie_args", return_value=[])
    @patch("subprocess.run")
    def test_failure_raises(self, mock_run, mock_cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="ERROR: not found"
        )
        with pytest.raises(DownloadError, match="Playlist metadata fetch failed"):
            get_playlist_metadata("https://youtube.com/playlist?list=PL123")

    @patch("mediasync.downloader.cookie_args", return_value=[])
    @patch("subprocess.run")
    def test_empty_output_raises(self, mock_run, mock_cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with pytest.raises(DownloadError, match="empty or unavailable"):
            get_playlist_metadata("https://youtube.com/playlist?list=PL123")


class TestGetFullPlaylistMetadata:
    @patch("mediasync.downloader.cookie_args", return_value=[])
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

    @patch("mediasync.downloader.cookie_args", return_value=[])
    @patch("subprocess.run")
    def test_failure_raises(self, mock_run, mock_cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="ERROR"
        )
        with pytest.raises(DownloadError, match="Full playlist metadata"):
            get_full_playlist_metadata("https://youtube.com/playlist?list=PL123")

    @patch("mediasync.downloader.cookie_args", return_value=[])
    @patch("subprocess.run")
    def test_empty_raises(self, mock_run, mock_cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with pytest.raises(DownloadError, match="empty or unavailable"):
            get_full_playlist_metadata("https://youtube.com/playlist?list=PL123")

    @patch("mediasync.downloader.cookie_args", return_value=[])
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


class TestRemoteComponentsAlwaysPassed:
    """Every yt-dlp subprocess must allow the JS challenge solver to be fetched.

    MediaSync originally built its yt-dlp argv by hand and omitted
    ``--remote-components``. yt-dlp 2026.07.04+ ships the YouTube n-challenge
    solver as an opt-in remote component, so without the flag it cannot
    de-cipher media URLs and returns storyboard images only. That went
    unnoticed because the podcast pipeline warms the same on-disk solver cache;
    on a cold host MediaSync downloads fail with "Requested format is not
    available". These tests fail if any invocation regresses.
    """

    @staticmethod
    def _assert_allows_remote_components(cmd):
        assert "--remote-components" in cmd, cmd
        assert cmd[cmd.index("--remote-components") + 1] == "ejs:github"

    @patch("mediasync.downloader.cookie_args", return_value=[])
    @patch("subprocess.run")
    def test_get_metadata(self, mock_run, _cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"id": "x"}), stderr=""
        )
        get_metadata("https://youtube.com/watch?v=uncached")
        self._assert_allows_remote_components(mock_run.call_args[0][0])

    @patch("mediasync.downloader.cookie_args", return_value=[])
    @patch("subprocess.run")
    def test_get_playlist_metadata(self, mock_run, _cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"id": "x"}), stderr=""
        )
        get_playlist_metadata("https://youtube.com/playlist?list=PL1")
        self._assert_allows_remote_components(mock_run.call_args[0][0])

    @patch("mediasync.downloader.cookie_args", return_value=[])
    @patch("subprocess.run")
    def test_get_full_playlist_metadata(self, mock_run, _cookies):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"id": "x"}), stderr=""
        )
        get_full_playlist_metadata("https://youtube.com/playlist?list=PL1")
        self._assert_allows_remote_components(mock_run.call_args[0][0])

    @pytest.mark.parametrize("fmt", ["audio", "video"])
    def test_build_cmd(self, fmt):
        with patch("mediasync.downloader.cookie_args", return_value=[]):
            cmd = _build_cmd("https://youtube.com/watch?v=x", fmt, "/out/%(title)s.%(ext)s")
        self._assert_allows_remote_components(cmd)

    def test_disabling_via_env_omits_the_flag(self, monkeypatch):
        """An explicit opt-out must not inject a contradictory flag."""
        monkeypatch.setenv("YTDLP_REMOTE_COMPONENTS", "")
        with patch("mediasync.downloader.cookie_args", return_value=[]):
            cmd = _build_cmd("https://youtube.com/watch?v=x", "audio", "/out/x.m4a")
        assert "--remote-components" not in cmd

    def test_env_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("YTDLP_REMOTE_COMPONENTS", "ejs:npm")
        with patch("mediasync.downloader.cookie_args", return_value=[]):
            cmd = _build_cmd("https://youtube.com/watch?v=x", "audio", "/out/x.m4a")
        assert cmd[cmd.index("--remote-components") + 1] == "ejs:npm"


class TestCookiesFlowThroughSharedDiscovery:
    """MediaSync must use the same cookie discovery as the podcast pipeline.

    It previously had its own finder that searched ``cwd`` and accepted any
    non-empty file, so running MediaSync from outside the repository root
    silently dropped authentication and produced bot-detection failures.
    """

    @patch("subprocess.run")
    def test_shared_discovery_is_used(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"id": "x"}), stderr=""
        )
        with patch("ytdlp_cookies.get_cookies_path", return_value="/shared/cookies.txt"):
            get_playlist_metadata("https://youtube.com/playlist?list=PLshared")
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--cookies") + 1] == "/shared/cookies.txt"

    @patch("subprocess.run")
    def test_no_cookies_omits_the_flag(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"id": "x"}), stderr=""
        )
        with patch("ytdlp_cookies.get_cookies_path", return_value=None):
            get_playlist_metadata("https://youtube.com/playlist?list=PLnone")
        assert "--cookies" not in mock_run.call_args[0][0]
