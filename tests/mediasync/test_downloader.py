"""Tests for mediasync.downloader module."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mediasync.downloader import (
    DownloadError,
    DownloadResult,
    DurationExceededError,
    MissingDownloadError,
    _build_cmd,
    _discard_partials,
    _find_output,
    _sanitize_title,
    cleanup_results,
    clear_metadata_cache,
    download,
    get_metadata,
)
from mediasync.notion_client import Format


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure metadata cache doesn't leak between tests."""
    clear_metadata_cache()
    yield
    clear_metadata_cache()


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


class TestIsPlaylist:
    def test_playlist_url(self):
        from mediasync.downloader import is_playlist
        assert is_playlist("https://www.youtube.com/playlist?list=PLxxx") is True

    def test_video_with_list_param(self):
        from mediasync.downloader import is_playlist
        # watch?v=X&list=Y is a video in a playlist context, treat as single
        assert is_playlist("https://www.youtube.com/watch?v=abc&list=PLxxx") is False

    def test_single_video_url(self):
        from mediasync.downloader import is_playlist
        assert is_playlist("https://www.youtube.com/watch?v=abc") is False


class TestGetPlaylistMetadata:
    def test_successful_playlist_fetch(self):
        from mediasync.downloader import get_playlist_metadata

        entries = [
            {"id": "vid1", "title": "Song 1"},
            {"id": "vid2", "title": "Song 2"},
        ]
        result = MagicMock()
        result.returncode = 0
        result.stdout = "\n".join(json.dumps(e) for e in entries)

        with patch("subprocess.run", return_value=result):
            data = get_playlist_metadata("https://youtube.com/playlist?list=PLxxx")

        assert len(data) == 2
        assert data[0]["title"] == "Song 1"

    def test_empty_playlist_raises(self):
        from mediasync.downloader import get_playlist_metadata

        result = MagicMock()
        result.returncode = 0
        result.stdout = ""

        with patch("subprocess.run", return_value=result):
            with pytest.raises(DownloadError, match="empty"):
                get_playlist_metadata("https://youtube.com/playlist?list=PLxxx")

    def test_failed_fetch_raises(self):
        from mediasync.downloader import get_playlist_metadata

        result = MagicMock()
        result.returncode = 1
        result.stderr = "ERROR: playlist not found"

        with patch("subprocess.run", return_value=result):
            with pytest.raises(DownloadError, match="Playlist metadata"):
                get_playlist_metadata("https://youtube.com/playlist?list=PLxxx")


class TestDownloadPlaylist:
    def test_playlist_downloads_all_items(self, tmp_path):
        """Playlist with 2 videos downloads both."""
        playlist_entries = [
            {"id": "vid1", "title": "Song 1"},
            {"id": "vid2", "title": "Song 2"},
        ]
        meta1 = {"title": "Song 1", "duration": 200, "uploader": "Artist", "thumbnail": ""}
        meta2 = {"title": "Song 2", "duration": 180, "uploader": "Artist", "thumbnail": ""}

        # Create fake output files
        (tmp_path / "Song 1.m4a").write_bytes(b"audio1")
        (tmp_path / "Song 2.m4a").write_bytes(b"audio2")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--flat-playlist" in cmd:
                result.stdout = "\n".join(json.dumps(e) for e in playlist_entries)
            elif "--dump-json" in cmd:
                if "vid1" in cmd[-1]:
                    result.stdout = json.dumps(meta1)
                else:
                    result.stdout = json.dumps(meta2)
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=mock_run):
            results = download(
                "https://youtube.com/playlist?list=PLxxx",
                Format.AUDIO,
                output_dir=str(tmp_path),
                max_duration_secs=7200,
            )

        assert len(results) == 2
        assert results[0].title == "Song 1"
        assert results[1].title == "Song 2"

    def test_playlist_skips_too_long_items(self, tmp_path):
        """Playlist items exceeding duration are skipped, not fatal."""
        playlist_entries = [
            {"id": "vid1", "title": "Short"},
            {"id": "vid2", "title": "Very Long"},
        ]
        meta_short = {"title": "Short", "duration": 100, "uploader": "A", "thumbnail": ""}
        meta_long = {"title": "Very Long", "duration": 99999, "uploader": "A", "thumbnail": ""}

        (tmp_path / "Short.m4a").write_bytes(b"audio")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--flat-playlist" in cmd:
                result.stdout = "\n".join(json.dumps(e) for e in playlist_entries)
            elif "--dump-json" in cmd:
                if "vid1" in cmd[-1]:
                    result.stdout = json.dumps(meta_short)
                else:
                    result.stdout = json.dumps(meta_long)
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=mock_run):
            results = download(
                "https://youtube.com/playlist?list=PLxxx",
                Format.AUDIO,
                output_dir=str(tmp_path),
                max_duration_secs=7200,
            )

        # Only the short one succeeds
        assert len(results) == 1
        assert results[0].title == "Short"

    def test_playlist_download_failure_raises(self, tmp_path):
        """If a playlist item fails download, raise immediately."""
        playlist_entries = [{"id": "vid1", "title": "Song"}]
        meta = {"title": "Song", "duration": 100, "uploader": "A", "thumbnail": ""}

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if "--flat-playlist" in cmd:
                result.returncode = 0
                result.stdout = json.dumps(playlist_entries[0])
            elif "--dump-json" in cmd:
                result.returncode = 0
                result.stdout = json.dumps(meta)
            else:
                result.returncode = 1
                result.stderr = "ERROR: 403 Forbidden"
            return result

        with patch("subprocess.run", side_effect=mock_run):
            with pytest.raises(DownloadError, match="403"):
                download(
                    "https://youtube.com/playlist?list=PLxxx",
                    Format.AUDIO,
                    output_dir=str(tmp_path),
                    max_duration_secs=7200,
                )

    def test_single_video_not_treated_as_playlist(self, tmp_path):
        """A regular video URL goes through single-video path."""
        meta = {"title": "Video", "duration": 60, "uploader": "Chan", "thumbnail": ""}
        (tmp_path / "Video.m4a").write_bytes(b"data")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--dump-json" in cmd:
                result.stdout = json.dumps(meta)
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=mock_run):
            results = download(
                "https://youtube.com/watch?v=abc",
                Format.AUDIO,
                output_dir=str(tmp_path),
                max_duration_secs=7200,
            )

        assert len(results) == 1
        assert results[0].title == "Video"


class TestPlaylistFilenameCollisions:
    """Two playlist items sharing a title must not overwrite each other (Fix #15)."""

    @staticmethod
    def _mock_run(tmp_path, playlist_entries, metas):
        """Simulate yt-dlp: metadata from *metas*, downloads write the -o path."""

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "--flat-playlist" in cmd:
                result.stdout = "\n".join(json.dumps(e) for e in playlist_entries)
            elif "--dump-json" in cmd:
                vid = cmd[-1].rsplit("=", 1)[-1]
                result.stdout = json.dumps(metas[vid])
            else:
                out = Path(cmd[cmd.index("-o") + 1])
                out.write_bytes(b"audio for " + out.stem.encode())
                result.stdout = ""
            return result

        return mock_run

    def test_same_titled_items_get_distinct_files(self, tmp_path):
        entries = [{"id": "vid1", "title": "Intro"}, {"id": "vid2", "title": "Intro"}]
        metas = {
            "vid1": {"title": "Intro", "duration": 10, "uploader": "A", "thumbnail": ""},
            "vid2": {"title": "Intro", "duration": 20, "uploader": "A", "thumbnail": ""},
        }

        with patch("subprocess.run", side_effect=self._mock_run(tmp_path, entries, metas)):
            results = download(
                "https://youtube.com/playlist?list=PLxxx",
                Format.AUDIO,
                output_dir=str(tmp_path),
                max_duration_secs=7200,
            )

        assert len(results) == 2
        paths = [r.path for r in results]
        assert paths[0] != paths[1], "second item overwrote the first"
        assert all(p.exists() for p in paths)
        # Titles reported to Notion stay human-readable
        assert [r.title for r in results] == ["Intro", "Intro"]

    def test_three_same_titled_items_are_all_distinct(self, tmp_path):
        entries = [{"id": f"vid{i}", "title": "Part 1"} for i in range(1, 4)]
        metas = {
            f"vid{i}": {"title": "Part 1", "duration": 10, "uploader": "A", "thumbnail": ""} for i in range(1, 4)
        }

        with patch("subprocess.run", side_effect=self._mock_run(tmp_path, entries, metas)):
            results = download(
                "https://youtube.com/playlist?list=PLxxx",
                Format.AUDIO,
                output_dir=str(tmp_path),
                max_duration_secs=7200,
            )

        assert len({r.path for r in results}) == 3

    def test_audio_and_video_of_one_item_share_a_stem(self, tmp_path):
        entries = [{"id": "vid1", "title": "Clip"}]
        metas = {"vid1": {"title": "Clip", "duration": 10, "uploader": "A", "thumbnail": ""}}

        with patch("subprocess.run", side_effect=self._mock_run(tmp_path, entries, metas)):
            results = download(
                "https://youtube.com/playlist?list=PLxxx",
                Format.BOTH,
                output_dir=str(tmp_path),
                max_duration_secs=7200,
            )

        assert {r.path.name for r in results} == {"Clip.m4a", "Clip.mp4"}

    def test_single_video_filename_is_unchanged(self, tmp_path):
        """Non-playlist downloads keep the plain "{title}.{ext}" name."""
        meta = {"title": "Solo", "duration": 60, "uploader": "C", "thumbnail": ""}

        with patch(
            "subprocess.run",
            side_effect=self._mock_run(tmp_path, [], {"abc": meta}),
        ):
            results = download(
                "https://youtube.com/watch?v=abc",
                Format.AUDIO,
                output_dir=str(tmp_path),
                max_duration_secs=7200,
            )

        assert results[0].path.name == "Solo.m4a"


class TestPartialDownloadCleanup:
    """A failed download must not leave media behind (Fix #15)."""

    def test_failed_single_download_removes_partials(self, tmp_path):
        meta = {"title": "Broken", "duration": 60, "uploader": "C", "thumbnail": ""}

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if "--dump-json" in cmd:
                result.returncode = 0
                result.stdout = json.dumps(meta)
                return result
            # Simulate yt-dlp writing a partial file, then failing
            (tmp_path / "Broken.m4a.part").write_bytes(b"half a file")
            result.returncode = 1
            result.stderr = "ERROR: 403 Forbidden"
            return result

        with patch("subprocess.run", side_effect=mock_run):
            with pytest.raises(DownloadError):
                download(
                    "https://youtube.com/watch?v=abc",
                    Format.AUDIO,
                    output_dir=str(tmp_path),
                    max_duration_secs=7200,
                )

        assert list(tmp_path.iterdir()) == [], "partial download was left on disk"

    def test_second_format_failure_removes_the_first_file(self, tmp_path):
        """Format.BOTH: audio succeeds, video fails — the audio file must go too."""
        meta = {"title": "Clip", "duration": 60, "uploader": "C", "thumbnail": ""}

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.stderr = ""
            if "--dump-json" in cmd:
                result.returncode = 0
                result.stdout = json.dumps(meta)
                return result
            out = Path(cmd[cmd.index("-o") + 1])
            if out.suffix == ".m4a":
                out.write_bytes(b"audio")
                result.returncode = 0
                return result
            result.returncode = 1
            result.stderr = "ERROR: video formats unavailable"
            return result

        with patch("subprocess.run", side_effect=mock_run):
            with pytest.raises(DownloadError):
                download(
                    "https://youtube.com/watch?v=abc",
                    Format.BOTH,
                    output_dir=str(tmp_path),
                    max_duration_secs=7200,
                )

        assert list(tmp_path.iterdir()) == []

    def test_playlist_failure_removes_earlier_items(self, tmp_path):
        entries = [{"id": "vid1", "title": "Good"}, {"id": "vid2", "title": "Bad"}]
        metas = {
            "vid1": {"title": "Good", "duration": 10, "uploader": "A", "thumbnail": ""},
            "vid2": {"title": "Bad", "duration": 10, "uploader": "A", "thumbnail": ""},
        }

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.stderr = ""
            result.returncode = 0
            if "--flat-playlist" in cmd:
                result.stdout = "\n".join(json.dumps(e) for e in entries)
                return result
            if "--dump-json" in cmd:
                result.stdout = json.dumps(metas[cmd[-1].rsplit("=", 1)[-1]])
                return result
            out = Path(cmd[cmd.index("-o") + 1])
            if out.stem == "Good":
                out.write_bytes(b"audio")
                return result
            result.returncode = 1
            result.stderr = "ERROR: 403 Forbidden"
            return result

        with patch("subprocess.run", side_effect=mock_run):
            with pytest.raises(DownloadError, match="403"):
                download(
                    "https://youtube.com/playlist?list=PLxxx",
                    Format.AUDIO,
                    output_dir=str(tmp_path),
                    max_duration_secs=7200,
                )

        assert list(tmp_path.iterdir()) == [], "earlier playlist items were leaked"

    def test_cleanup_results_tolerates_missing_files(self, tmp_path):
        present = tmp_path / "here.m4a"
        present.write_bytes(b"x")
        results = [
            DownloadResult(present, "t", "a", 1, "", "audio"),
            DownloadResult(tmp_path / "gone.m4a", "t", "a", 1, "", "audio"),
        ]

        cleanup_results(results)

        assert not present.exists()

    def test_discard_partials_only_touches_matching_stem(self, tmp_path):
        (tmp_path / "Keep.m4a").write_bytes(b"keep")
        (tmp_path / "Drop.m4a").write_bytes(b"drop")
        (tmp_path / "Drop.f140.m4a").write_bytes(b"drop")

        _discard_partials(str(tmp_path), "Drop")

        assert {p.name for p in tmp_path.iterdir()} == {"Keep.m4a"}

    def test_discard_partials_treats_stem_literally(self, tmp_path):
        """Titles containing glob metacharacters must not widen the deletion."""
        (tmp_path / "Song [live].m4a").write_bytes(b"a")
        (tmp_path / "Songs.m4a").write_bytes(b"b")

        _discard_partials(str(tmp_path), "Song [live]")

        assert {p.name for p in tmp_path.iterdir()} == {"Songs.m4a"}

    def test_discard_partials_spares_sibling_with_stem_as_prefix(self, tmp_path):
        """Regression: a retry must not delete another item's finished file.

        ``_claim_stem`` gives duplicate titles stems like ``Song`` and
        ``Song (2)``, and unrelated items may share a prefix (``Intro`` /
        ``Intro Part 1``).  A prefix glob deleted those siblings, after which
        the retry succeeded and the pipeline hit ENOENT at the tag/upload step.
        """
        (tmp_path / "Song.m4a").write_bytes(b"failing item")
        (tmp_path / "Song.part").write_bytes(b"failing item partial")
        (tmp_path / "Song (2).m4a").write_bytes(b"sibling, already done")
        (tmp_path / "Intro Part 1.m4a").write_bytes(b"unrelated, already done")

        _discard_partials(str(tmp_path), "Song")

        assert {p.name for p in tmp_path.iterdir()} == {
            "Song (2).m4a",
            "Intro Part 1.m4a",
        }

    def test_discard_partials_removes_format_and_temp_variants(self, tmp_path):
        for name in (
            "Song.m4a",
            "Song.part",
            "Song.ytdl",
            "Song.f140.m4a",
            "Song.m4a.part",
            "Song.webp",
        ):
            (tmp_path / name).write_bytes(b"x")
        (tmp_path / "Song.Live Session.m4a").write_bytes(b"different title")

        _discard_partials(str(tmp_path), "Song")

        assert {p.name for p in tmp_path.iterdir()} == {"Song.Live Session.m4a"}

    def test_discard_partials_missing_dir_is_noop(self, tmp_path):
        _discard_partials(str(tmp_path / "nope"), "Song")

    def test_find_output_matches_title_with_glob_metacharacters(self, tmp_path):
        """Regression: an unescaped glob made bracketed titles look missing."""
        actual = tmp_path / "Song [live].opus"
        actual.write_bytes(b"x")

        assert _find_output(tmp_path / "Song [live].m4a") == actual

    def test_download_raises_when_result_file_vanished(self, tmp_path):
        """A file deleted between download and upload must fail loudly."""
        ghost = DownloadResult(
            path=tmp_path / "gone.m4a",
            title="Gone",
            artist="A",
            duration_secs=10,
            thumbnail_url="",
            format_type="audio",
        )
        with patch("mediasync.downloader.is_playlist", return_value=False), patch(
            "mediasync.downloader._download_single", return_value=[ghost]
        ):
            with pytest.raises(MissingDownloadError) as exc:
                download(
                    "https://youtu.be/x",
                    Format.AUDIO,
                    output_dir=str(tmp_path),
                    max_duration_secs=99999,
                )
        assert "Gone" in str(exc.value)
        assert "gone.m4a" in str(exc.value)

    def test_download_passes_through_when_files_present(self, tmp_path):
        real = tmp_path / "here.m4a"
        real.write_bytes(b"x")
        ok = DownloadResult(
            path=real,
            title="Here",
            artist="A",
            duration_secs=10,
            thumbnail_url="",
            format_type="audio",
        )
        with patch("mediasync.downloader.is_playlist", return_value=False), patch(
            "mediasync.downloader._download_single", return_value=[ok]
        ):
            assert download(
                "https://youtu.be/x",
                Format.AUDIO,
                output_dir=str(tmp_path),
                max_duration_secs=99999,
            ) == [ok]

    def test_find_output_prefers_exact_extension_over_format_leftover(self, tmp_path):
        (tmp_path / "Song.f140.m4a").write_bytes(b"leftover")
        real = tmp_path / "Song.opus"
        real.write_bytes(b"real")

        assert _find_output(tmp_path / "Song.m4a") == real
