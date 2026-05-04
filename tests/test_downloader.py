"""Unit tests for the audio downloader module."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from downloader import DownloadError, download_and_convert


class TestDownloadAndConvertSuccess:
    """5.1 / 5.2 — Successful download and conversion."""

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_returns_mp3_path(self, mock_ydl_cls):
        """After a successful download the function returns the MP3 path."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "testvid"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")

            # Simulate yt_dlp creating the MP3 file
            def fake_download(urls):
                with open(mp3_path, "wb") as f:
                    f.write(b"\xff\xfb\x90\x00" * 100)  # fake MP3 bytes

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = fake_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            result = download_and_convert(
                "https://youtube.com/watch?v=testvid", video_id, tmp_dir
            )

            assert result == mp3_path
            assert os.path.exists(result)
            assert os.path.getsize(result) > 0

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_ydl_options_configured_correctly(self, mock_ydl_cls):
        """yt_dlp should be configured with bestaudio and FFmpeg postprocessor."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "cfgtest"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")

            def fake_download(urls):
                with open(mp3_path, "wb") as f:
                    f.write(b"\xff\xfb\x90\x00" * 100)

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = fake_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            download_and_convert(
                "https://youtube.com/watch?v=cfgtest", video_id, tmp_dir
            )

            opts = mock_ydl_cls.call_args[0][0]
            assert opts["format"] == "18/bestaudio/best"
            assert opts["quiet"] is True
            assert opts["no_warnings"] is True

            # Check FFmpeg postprocessor
            pp = opts["postprocessors"]
            assert len(pp) == 1
            assert pp[0]["key"] == "FFmpegExtractAudio"
            assert pp[0]["preferredcodec"] == "mp3"
            assert pp[0]["preferredquality"] == "192"

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_output_template_uses_video_id(self, mock_ydl_cls):
        """Output template should be {tmp_dir}/{video_id}.%(ext)s."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "tmpltest"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")

            def fake_download(urls):
                with open(mp3_path, "wb") as f:
                    f.write(b"\xff\xfb\x90\x00" * 100)

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = fake_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            download_and_convert(
                "https://youtube.com/watch?v=tmpltest", video_id, tmp_dir
            )

            opts = mock_ydl_cls.call_args[0][0]
            expected_template = os.path.join(tmp_dir, f"{video_id}.%(ext)s")
            assert opts["outtmpl"] == expected_template


class TestIntermediateFileCleanup:
    """5.2 — Intermediate files (webm, opus, m4a) are cleaned up."""

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_intermediate_files_removed_after_success(self, mock_ydl_cls):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "cleantest"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")

            def fake_download(urls):
                # Simulate yt_dlp leaving intermediate files
                with open(mp3_path, "wb") as f:
                    f.write(b"\xff\xfb\x90\x00" * 100)
                # Create intermediate files
                for ext in ["webm", "opus", "m4a"]:
                    with open(
                        os.path.join(tmp_dir, f"{video_id}.{ext}"), "wb"
                    ) as f:
                        f.write(b"intermediate")

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = fake_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            result = download_and_convert(
                "https://youtube.com/watch?v=cleantest", video_id, tmp_dir
            )

            # MP3 should exist
            assert os.path.exists(result)
            # Intermediate files should be gone
            for ext in ["webm", "opus", "m4a"]:
                assert not os.path.exists(
                    os.path.join(tmp_dir, f"{video_id}.{ext}")
                )


class TestDownloadFailureHandling:
    """5.3 — Failures raise DownloadError after cleaning up partial files."""

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_download_error_raised_on_ydl_exception(self, mock_ydl_cls):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "failtest"

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = Exception("Network error")
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            with pytest.raises(DownloadError, match="Failed to download/convert"):
                download_and_convert(
                    "https://youtube.com/watch?v=failtest", video_id, tmp_dir
                )

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_partial_files_cleaned_on_failure(self, mock_ydl_cls):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "partialclean"

            def failing_download(urls):
                # Create partial files before failing
                for ext in ["webm", "mp3"]:
                    with open(
                        os.path.join(tmp_dir, f"{video_id}.{ext}"), "wb"
                    ) as f:
                        f.write(b"partial")
                raise Exception("Conversion failed")

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = failing_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            with pytest.raises(DownloadError):
                download_and_convert(
                    "https://youtube.com/watch?v=partialclean",
                    video_id,
                    tmp_dir,
                )

            # All partial files should be cleaned up
            assert not os.path.exists(
                os.path.join(tmp_dir, f"{video_id}.webm")
            )
            assert not os.path.exists(
                os.path.join(tmp_dir, f"{video_id}.mp3")
            )

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_error_raised_when_mp3_missing_after_download(self, mock_ydl_cls):
        """If yt_dlp succeeds but no MP3 is produced, raise DownloadError."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "nomp3"

            # yt_dlp "succeeds" but doesn't create the MP3
            mock_ydl = MagicMock()
            mock_ydl.download.return_value = None
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            with pytest.raises(DownloadError, match="MP3 file not found"):
                download_and_convert(
                    "https://youtube.com/watch?v=nomp3", video_id, tmp_dir
                )

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_error_raised_when_mp3_is_empty(self, mock_ydl_cls):
        """If the MP3 file exists but is 0 bytes, raise DownloadError."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "emptymp3"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")

            def fake_download(urls):
                # Create an empty MP3 file
                open(mp3_path, "wb").close()

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = fake_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            with pytest.raises(DownloadError, match="MP3 file is empty"):
                download_and_convert(
                    "https://youtube.com/watch?v=emptymp3", video_id, tmp_dir
                )

            # Empty MP3 should be cleaned up
            assert not os.path.exists(mp3_path)

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_error_message_includes_video_id(self, mock_ydl_cls):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "errmsg123"

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = Exception("timeout")
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            with pytest.raises(DownloadError, match="errmsg123"):
                download_and_convert(
                    "https://youtube.com/watch?v=errmsg123", video_id, tmp_dir
                )


class TestOsErrorHandling:
    """OSError in cleanup/remove branches should be silently swallowed."""

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.os.remove")
    def test_cleanup_intermediate_oserror_is_swallowed(self, mock_remove, mock_ydl_cls):
        """OSError when removing intermediate file should not propagate."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "oserr1"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")
            webm_path = os.path.join(tmp_dir, f"{video_id}.webm")

            def fake_download(urls):
                with open(mp3_path, "wb") as f:
                    f.write(b"\xff\xfb\x90\x00" * 100)
                with open(webm_path, "wb") as f:
                    f.write(b"intermediate")

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = fake_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            # Make os.remove raise OSError for non-mp3 files but succeed for mp3
            def selective_remove(path):
                if not path.endswith(".mp3"):
                    raise OSError("permission denied")
                # For mp3 just do nothing (file still exists for the check)

            mock_remove.side_effect = selective_remove

            # Should not raise even though cleanup fails
            with patch("downloader.os.path.exists", return_value=True), \
                 patch("downloader.os.path.getsize", return_value=400):
                result = download_and_convert(
                    f"https://youtube.com/watch?v={video_id}", video_id, tmp_dir
                )
            assert result == mp3_path

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.os.remove")
    def test_partial_mp3_oserror_on_failure_is_swallowed(self, mock_remove, mock_ydl_cls):
        """OSError when removing partial mp3 after download failure should not propagate."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "oserr2"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")

            def failing_download(urls):
                with open(mp3_path, "wb") as f:
                    f.write(b"partial")
                raise Exception("Download failed")

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = failing_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            # os.remove raises OSError
            mock_remove.side_effect = OSError("locked")

            from downloader import DownloadError
            with pytest.raises(DownloadError):
                with patch("downloader.os.path.exists", return_value=True):
                    download_and_convert(
                        f"https://youtube.com/watch?v={video_id}", video_id, tmp_dir
                    )

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.os.remove")
    def test_empty_mp3_oserror_on_cleanup_is_swallowed(self, mock_remove, mock_ydl_cls):
        """OSError when removing empty mp3 should not propagate (DownloadError still raised)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "oserr3"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")

            def fake_download(urls):
                open(mp3_path, "wb").close()  # empty file

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = fake_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            # os.remove raises OSError for empty mp3 cleanup
            mock_remove.side_effect = OSError("locked")

            from downloader import DownloadError
            with pytest.raises(DownloadError, match="empty"):
                with patch("downloader.os.path.exists", return_value=True), \
                     patch("downloader.os.path.getsize", return_value=0):
                    download_and_convert(
                        f"https://youtube.com/watch?v={video_id}", video_id, tmp_dir
                    )


class TestFfmpegPathSetup:
    """FFmpeg PATH setup for Lambda layer."""

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.os.path.isdir")
    def test_adds_opt_bin_to_path_when_exists(self, mock_isdir, mock_ydl_cls):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "pathtest"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")

            def fake_download(urls):
                with open(mp3_path, "wb") as f:
                    f.write(b"\xff\xfb\x90\x00" * 100)

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = fake_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            mock_isdir.return_value = True
            original_path = os.environ.get("PATH", "")

            # Remove /opt/bin if it's already there to test the addition
            cleaned_path = os.pathsep.join(
                p for p in original_path.split(os.pathsep) if p != "/opt/bin"
            )
            os.environ["PATH"] = cleaned_path

            try:
                download_and_convert(
                    "https://youtube.com/watch?v=pathtest", video_id, tmp_dir
                )
                assert "/opt/bin" in os.environ["PATH"]
            finally:
                os.environ["PATH"] = original_path
