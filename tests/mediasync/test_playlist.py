"""Tests for mediasync.playlist module."""


from mediasync.playlist import (
    _relative_posix,
    _sanitize_filename,
    generate_m3u,
    make_relative_keys,
)


class TestGenerateM3u:
    def test_generates_valid_m3u8(self, tmp_path):
        items = [
            {"remote_key": "../audio/Song One.m4a", "title": "Song One", "artist": "Artist A", "duration_secs": 240},
            {"remote_key": "../audio/Song Two.m4a", "title": "Song Two", "artist": "Artist B", "duration_secs": 180},
        ]
        result = generate_m3u("My Playlist", items, str(tmp_path))

        assert result.name == "My Playlist.m3u8"
        assert result.exists()

        content = result.read_text()
        assert content.startswith("#EXTM3U\n")
        assert "#PLAYLIST:My Playlist\n" in content
        assert "#EXTINF:240,Artist A - Song One\n" in content
        assert "../audio/Song One.m4a\n" in content
        assert "#EXTINF:180,Artist B - Song Two\n" in content
        assert "../audio/Song Two.m4a\n" in content

    def test_unknown_artist_omits_prefix(self, tmp_path):
        items = [
            {"remote_key": "track.m4a", "title": "Track", "artist": "Unknown", "duration_secs": 60},
        ]
        result = generate_m3u("Test", items, str(tmp_path))
        content = result.read_text()
        assert "#EXTINF:60,Track\n" in content

    def test_creates_output_dir(self, tmp_path):
        out = tmp_path / "sub" / "dir"
        items = [{"remote_key": "x.m4a", "title": "X", "artist": "A", "duration_secs": 10}]
        result = generate_m3u("Test", items, str(out))
        assert result.exists()

    def test_sanitizes_filename(self, tmp_path):
        items = [{"remote_key": "x.m4a", "title": "X", "artist": "A", "duration_secs": 10}]
        result = generate_m3u("Bad/Name:Here?", items, str(tmp_path))
        assert "/" not in result.name
        assert ":" not in result.name
        assert "?" not in result.name


class TestMakeRelativeKeys:
    def test_sibling_folders(self):
        playlist_folder = "MediaSync/harshit/playlists"
        keys = [
            "MediaSync/harshit/audio/Song.m4a",
            "MediaSync/harshit/video/Clip.mp4",
        ]
        result = make_relative_keys(playlist_folder, keys)
        assert result == ["../audio/Song.m4a", "../video/Clip.mp4"]

    def test_same_folder(self):
        result = make_relative_keys("a/b", ["a/b/file.m4a"])
        assert result == ["file.m4a"]

    def test_deeply_nested(self):
        result = make_relative_keys("root/sub/playlists", ["root/sub/audio/deep/file.m4a"])
        assert result == ["../audio/deep/file.m4a"]


class TestRelativePosix:
    def test_basic(self):
        from pathlib import PurePosixPath
        result = _relative_posix(PurePosixPath("a/b/c.txt"), PurePosixPath("a/d"))
        assert str(result) == "../b/c.txt"

    def test_same_dir(self):
        from pathlib import PurePosixPath
        result = _relative_posix(PurePosixPath("a/b/file.m4a"), PurePosixPath("a/b"))
        assert str(result) == "file.m4a"


class TestSanitizeFilename:
    def test_removes_unsafe_chars(self):
        assert _sanitize_filename("Hello: World?") == "Hello World"

    def test_truncates_long_names(self):
        result = _sanitize_filename("A" * 200)
        assert len(result) == 150

    def test_empty_returns_playlist(self):
        assert _sanitize_filename("") == "Playlist"
        assert _sanitize_filename("???") == "Playlist"
