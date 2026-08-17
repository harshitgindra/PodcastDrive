"""Tests for OneDrive health check and token rotation."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mediasync.onedrive_client import OneDriveClient, OneDriveError


@pytest.fixture
def mock_token_response():
    """Mock a successful token refresh response."""
    return json.dumps({
        "access_token": "new_access_token",
        "refresh_token": "new_refresh_token",
        "expires_in": 3600,
    }).encode()


class TestCheckHealth:
    @patch("urllib.request.urlopen")
    def test_healthy_returns_true(self, mock_urlopen, mock_token_response):
        # Mock token refresh
        token_resp = MagicMock()
        token_resp.read.return_value = mock_token_response
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        # Mock health check (drive info)
        health_resp = MagicMock()
        health_resp.read.return_value = json.dumps({
            "quota": {"used": 1000000, "total": 5000000000}
        }).encode()
        health_resp.__enter__ = lambda s: s
        health_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [token_resp, health_resp]

        client = OneDriveClient("id", "secret", "refresh")
        assert client.check_health() is True

    @patch("urllib.request.urlopen")
    def test_401_returns_false(self, mock_urlopen, mock_token_response):
        import urllib.error

        token_resp = MagicMock()
        token_resp.read.return_value = mock_token_response
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [
            token_resp,
            urllib.error.HTTPError("url", 401, "Unauthorized", {}, None),
        ]

        client = OneDriveClient("id", "secret", "refresh")
        assert client.check_health() is False

    @patch("urllib.request.urlopen")
    def test_network_error_returns_false(self, mock_urlopen, mock_token_response):
        token_resp = MagicMock()
        token_resp.read.return_value = mock_token_response
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [token_resp, OSError("Network down")]

        client = OneDriveClient("id", "secret", "refresh")
        assert client.check_health() is False


class TestCurrentRefreshToken:
    @patch("urllib.request.urlopen")
    def test_returns_rotated_token(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "access_token": "at",
            "refresh_token": "rotated_token",
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        client = OneDriveClient("id", "secret", "original_token")
        assert client.current_refresh_token == "rotated_token"


class TestPersistRotatedToken:
    @patch("urllib.request.urlopen")
    def test_updates_env_file(self, mock_urlopen, tmp_path, monkeypatch):
        env_file = tmp_path / "mediasync.env"
        env_file.write_text(
            "MEDIASYNC_NOTION_TOKEN=abc\n"
            "MEDIASYNC_ONEDRIVE_REFRESH_TOKEN=old_token\n"
            "MEDIASYNC_STORAGE=onedrive\n"
        )
        monkeypatch.chdir(tmp_path)

        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "access_token": "at",
            "refresh_token": "brand_new_token",
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        client = OneDriveClient("id", "secret", "old_token")

        content = env_file.read_text()
        assert "MEDIASYNC_ONEDRIVE_REFRESH_TOKEN=brand_new_token" in content
        assert "old_token" not in content

    @patch("urllib.request.urlopen")
    def test_handles_export_prefix(self, mock_urlopen, tmp_path, monkeypatch):
        env_file = tmp_path / "mediasync.env"
        env_file.write_text(
            "export MEDIASYNC_ONEDRIVE_REFRESH_TOKEN=old_token\n"
        )
        monkeypatch.chdir(tmp_path)

        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "access_token": "at",
            "refresh_token": "new_token",
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        OneDriveClient("id", "secret", "old_token")

        content = env_file.read_text()
        assert "export MEDIASYNC_ONEDRIVE_REFRESH_TOKEN=new_token" in content

    @patch("urllib.request.urlopen")
    def test_no_env_file_is_silent(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # No mediasync.env exists

        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "access_token": "at",
            "refresh_token": "new_token",
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        # Should not raise
        client = OneDriveClient("id", "secret", "old_token")
        assert client.current_refresh_token == "new_token"