"""Unit tests for src/ytdlp_cookies.py."""

from unittest.mock import patch

from ytdlp_cookies import get_cookies_path, inject_cookies


class TestGetCookiesPath:
    def test_returns_none_when_no_valid_cookies_exist(self):
        """Covers the `return None` path when no cookies.txt found."""
        # Mock Path so no candidate file passes the exists()+size check
        with patch("ytdlp_cookies.Path") as MockPath:
            mock_candidate = MockPath.return_value.parent.parent.__truediv__.return_value
            mock_candidate.exists.return_value = False
            MockPath.home.return_value.__truediv__.return_value.__truediv__.return_value.exists.return_value = False
            result = get_cookies_path()
        assert result is None


class TestInjectCookies:
    def test_does_not_overwrite_existing_cookiefile(self):
        opts = {"cookiefile": "/existing/path.txt"}
        result = inject_cookies(opts)
        assert result["cookiefile"] == "/existing/path.txt"

    def test_adds_cookiefile_when_path_found(self):
        with patch("ytdlp_cookies.get_cookies_path", return_value="/tmp/cookies.txt"):
            result = inject_cookies({})
            assert result["cookiefile"] == "/tmp/cookies.txt"

    def test_no_op_when_no_cookies_found(self):
        with patch("ytdlp_cookies.get_cookies_path", return_value=None):
            opts = {"format": "best"}
            result = inject_cookies(opts)
            assert "cookiefile" not in result
