"""Tests for mediasync.tagger module."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from mediasync.tagger import tag_file


class TestTagFile:
    def test_mp4_extension(self, tmp_path):
        path = tmp_path / "song.m4a"
        path.write_bytes(b"fake")

        with patch("mediasync.tagger._tag_mp4") as mock_mp4:
            tag_file(path, "Title", "Artist")
        mock_mp4.assert_called_once_with(path, "Title", "Artist")

    def test_mp4_video_extension(self, tmp_path):
        path = tmp_path / "video.mp4"
        path.write_bytes(b"fake")

        with patch("mediasync.tagger._tag_mp4") as mock_mp4:
            tag_file(path, "Title", "Artist")
        mock_mp4.assert_called_once()

    def test_m4v_extension(self, tmp_path):
        path = tmp_path / "clip.m4v"
        path.write_bytes(b"fake")

        with patch("mediasync.tagger._tag_mp4") as mock_mp4:
            tag_file(path, "Title", "Artist")
        mock_mp4.assert_called_once()

    def test_mp3_extension(self, tmp_path):
        path = tmp_path / "song.mp3"
        path.write_bytes(b"fake")

        with patch("mediasync.tagger._tag_mp3") as mock_mp3:
            tag_file(path, "Title", "Artist")
        mock_mp3.assert_called_once_with(path, "Title", "Artist")

    def test_unsupported_extension_skips(self, tmp_path):
        path = tmp_path / "song.ogg"
        path.write_bytes(b"fake")
        # Should not raise
        tag_file(path, "Title", "Artist")

    def test_exception_is_caught(self, tmp_path):
        path = tmp_path / "song.m4a"
        path.write_bytes(b"fake")

        with patch("mediasync.tagger._tag_mp4", side_effect=Exception("corrupt file")):
            # Should not raise
            tag_file(path, "Title", "Artist")


class TestTagMp4:
    def test_sets_title_and_artist(self):
        mock_mp4 = MagicMock()
        with patch("mutagen.mp4.MP4", return_value=mock_mp4):
            from mediasync.tagger import _tag_mp4
            _tag_mp4(Path("/fake/song.m4a"), "My Song", "My Artist")

        mock_mp4.__setitem__.assert_any_call("\xa9nam", ["My Song"])
        mock_mp4.__setitem__.assert_any_call("\xa9ART", ["My Artist"])
        mock_mp4.save.assert_called_once()


class TestTagMp3:
    def test_sets_title_and_artist(self):
        mock_tags = MagicMock()
        with patch("mutagen.easyid3.EasyID3", return_value=mock_tags):
            from mediasync.tagger import _tag_mp3
            _tag_mp3(Path("/fake/song.mp3"), "My Song", "My Artist")

        mock_tags.__setitem__.assert_any_call("title", "My Song")
        mock_tags.__setitem__.assert_any_call("artist", "My Artist")
        mock_tags.save.assert_called_once()

    def test_creates_id3_header_if_missing(self):
        from mutagen.id3 import ID3NoHeaderError

        mock_id3 = MagicMock()
        mock_tags = MagicMock()

        with patch("mutagen.easyid3.EasyID3", side_effect=[ID3NoHeaderError(), mock_tags]):
            with patch("mutagen.id3.ID3", return_value=mock_id3):
                from mediasync.tagger import _tag_mp3
                _tag_mp3(Path("/fake/song.mp3"), "Title", "Artist")

        mock_id3.save.assert_called_once()
        mock_tags.save.assert_called_once()
