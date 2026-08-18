"""Additional OneDrive client tests for list_folder and _simple_upload retry."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from mediasync.onedrive_client import OneDriveClient


@pytest.fixture
def token_response():
    return json.dumps({"access_token": "fresh_token", "refresh_token": "new_refresh"}).encode()


@pytest.fixture
def make_client(token_response):
    def _make():
        with patch("urllib.request.urlopen") as mock_urlopen:
            resp = MagicMock()
            resp.read.return_value = token_response
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = resp
            client = OneDriveClient("cid", "cs", "refresh_tok")
        return client
    return _make


class TestListFolder:
    @patch("urllib.request.urlopen")
    def test_returns_filenames(self, mock_urlopen, make_client):
        client = make_client()

        response_data = json.dumps({
            "value": [
                {"name": "song1.m4a"},
                {"name": "song2.mp3"},
                {"name": "subfolder", "folder": {"childCount": 3}},  # skipped
            ],
        }).encode()

        resp = MagicMock()
        resp.read.return_value = response_data
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        result = client.list_folder("MediaSync/audio")
        assert result == {"song1.m4a", "song2.mp3"}

    @patch("urllib.request.urlopen")
    def test_pagination(self, mock_urlopen, make_client):
        client = make_client()

        page1 = json.dumps({
            "value": [{"name": "a.m4a"}],
            "@odata.nextLink": "https://graph.microsoft.com/next",
        }).encode()
        page2 = json.dumps({
            "value": [{"name": "b.m4a"}],
        }).encode()

        resp1 = MagicMock()
        resp1.read.return_value = page1
        resp1.__enter__ = lambda s: s
        resp1.__exit__ = MagicMock(return_value=False)

        resp2 = MagicMock()
        resp2.read.return_value = page2
        resp2.__enter__ = lambda s: s
        resp2.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [resp1, resp2]

        result = client.list_folder("MediaSync/audio")
        assert result == {"a.m4a", "b.m4a"}

    @patch("urllib.request.urlopen")
    def test_401_retries_with_refreshed_token(self, mock_urlopen, make_client):
        client = make_client()

        # First call: 401, triggers refresh
        http_err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)

        # Token refresh response
        token_resp = MagicMock()
        token_resp.read.return_value = json.dumps(
            {"access_token": "new_token", "refresh_token": "nr"}
        ).encode()
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        # Retry response (success)
        success_resp = MagicMock()
        success_resp.read.return_value = json.dumps({"value": [{"name": "file.m4a"}]}).encode()
        success_resp.__enter__ = lambda s: s
        success_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [http_err, token_resp, success_resp]

        result = client.list_folder("MediaSync/audio")
        assert result == {"file.m4a"}

    @patch("urllib.request.urlopen")
    def test_non_401_http_error_returns_partial(self, mock_urlopen, make_client):
        client = make_client()

        http_err = urllib.error.HTTPError("url", 500, "Server Error", {}, None)
        mock_urlopen.side_effect = http_err

        result = client.list_folder("MediaSync/audio")
        assert result == set()

    @patch("urllib.request.urlopen")
    def test_generic_exception_returns_empty(self, mock_urlopen, make_client):
        client = make_client()

        mock_urlopen.side_effect = OSError("network")
        result = client.list_folder("MediaSync/audio")
        assert result == set()


class TestSimpleUploadRetry:
    @patch("urllib.request.urlopen")
    def test_401_on_simple_upload_retries(self, mock_urlopen, make_client):
        client = make_client()

        # First upload: 401
        http_err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)

        # Token refresh
        token_resp = MagicMock()
        token_resp.read.return_value = json.dumps(
            {"access_token": "refreshed", "refresh_token": "nr"}
        ).encode()
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        # Retry upload: success
        upload_resp = MagicMock()
        upload_resp.__enter__ = lambda s: s
        upload_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [http_err, token_resp, upload_resp]

        # _simple_upload is a private method; call via upload with small file
        # that passes the file_exists check
        with patch.object(client, "file_exists", return_value=False):
            import tempfile
            from pathlib import Path

            with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
                f.write(b"small audio data")
                local_path = Path(f.name)

            try:
                result = client.upload(local_path, "folder", "test.m4a")
                assert result == "folder/test.m4a"
                assert client._access_token == "refreshed"
            finally:
                local_path.unlink(missing_ok=True)
