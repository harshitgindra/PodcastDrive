"""Tests for mediasync.artwork module."""

from pathlib import Path
from unittest.mock import patch

from mediasync.artwork import download_thumbnail


class TestDownloadThumbnail:
    def test_downloads_thumbnail(self, tmp_path):
        with patch("urllib.request.urlretrieve") as mock_retrieve:
            # Simulate urlretrieve writing a file
            def side_effect(url, path):
                Path(path).write_bytes(b"fake jpg data")
            mock_retrieve.side_effect = side_effect

            result = download_thumbnail("https://img.youtube.com/thumb.jpg", str(tmp_path))

        assert result is not None
        assert result.name == "folder.jpg"
        assert result.exists()

    def test_custom_filename(self, tmp_path):
        with patch("urllib.request.urlretrieve") as mock_retrieve:
            def side_effect(url, path):
                Path(path).write_bytes(b"data")
            mock_retrieve.side_effect = side_effect

            result = download_thumbnail("https://example.com/img.jpg", str(tmp_path), "cover.jpg")

        assert result is not None
        assert result.name == "cover.jpg"

    def test_empty_url_returns_none(self, tmp_path):
        result = download_thumbnail("", str(tmp_path))
        assert result is None

    def test_network_error_returns_none(self, tmp_path):
        with patch("urllib.request.urlretrieve", side_effect=OSError("timeout")):
            result = download_thumbnail("https://img.youtube.com/x.jpg", str(tmp_path))
        assert result is None

    def test_creates_output_dir(self, tmp_path):
        out = tmp_path / "sub" / "dir"
        with patch("urllib.request.urlretrieve") as mock_retrieve:
            def side_effect(url, path):
                Path(path).write_bytes(b"data")
            mock_retrieve.side_effect = side_effect

            result = download_thumbnail("https://example.com/img.jpg", str(out))

        assert result is not None
        assert out.exists()
