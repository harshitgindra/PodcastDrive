"""Tests for mediasync.url_handler module."""

import pytest

from mediasync.url_handler import (
    get_source_platform,
    is_apple_music_url,
    is_spotify_url,
    is_supported_url,
    is_youtube_url,
    normalize_url,
)


class TestIsYoutubeUrl:
    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtube.com/watch?v=abc123",
        "https://m.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://music.youtube.com/watch?v=abc123",
        "https://www.youtube.com/playlist?list=PLxyz",
    ])
    def test_youtube_urls(self, url):
        assert is_youtube_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://open.spotify.com/track/abc",
        "https://music.apple.com/album/123",
        "https://example.com/video",
    ])
    def test_non_youtube_urls(self, url):
        assert is_youtube_url(url) is False


class TestIsSpotifyUrl:
    @pytest.mark.parametrize("url", [
        "https://open.spotify.com/track/abc123",
        "https://open.spotify.com/album/xyz",
        "https://open.spotify.com/playlist/list123",
    ])
    def test_spotify_urls(self, url):
        assert is_spotify_url(url) is True

    def test_non_spotify(self):
        assert is_spotify_url("https://youtube.com/watch?v=x") is False


class TestIsAppleMusicUrl:
    def test_apple_music(self):
        assert is_apple_music_url("https://music.apple.com/us/album/song/123") is True

    def test_non_apple(self):
        assert is_apple_music_url("https://youtube.com/watch?v=x") is False


class TestGetSourcePlatform:
    def test_youtube(self):
        assert get_source_platform("https://www.youtube.com/watch?v=x") == "youtube"

    def test_spotify(self):
        assert get_source_platform("https://open.spotify.com/track/x") == "spotify"

    def test_apple_music(self):
        assert get_source_platform("https://music.apple.com/us/album/x") == "apple_music"

    def test_other(self):
        assert get_source_platform("https://example.com/video") == "other"


class TestNormalizeUrl:
    def test_http_url_passthrough(self):
        url = "https://open.spotify.com/track/abc"
        assert normalize_url(url) == url

    def test_youtube_url_passthrough(self):
        url = "https://www.youtube.com/watch?v=abc123"
        assert normalize_url(url) == url

    def test_plain_text_becomes_search(self):
        assert normalize_url("lofi hip hop") == "ytsearch:lofi hip hop"

    def test_strips_whitespace(self):
        assert normalize_url("  https://example.com  ") == "https://example.com"

    def test_search_with_special_chars(self):
        result = normalize_url("Artist - Song Title (Official)")
        assert result.startswith("ytsearch:")


class TestIsSupportedUrl:
    def test_http_url(self):
        assert is_supported_url("https://example.com/video") is True

    def test_search_query(self):
        assert is_supported_url("lofi beats") is True

    def test_empty_rejected(self):
        assert is_supported_url("") is False

    def test_too_short_rejected(self):
        assert is_supported_url("ab") is False

    def test_path_rejected(self):
        assert is_supported_url("/usr/bin/foo") is False