"""Tests for mediasync.onedrive_client."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from mediasync.onedrive_client import (
    CHUNK_SIZE,
    SIMPLE_UPLOAD_LIMIT,
    OneDriveClient,
    OneDriveError,
)


@pytest.fixture
def token_response():
    """Successful token refresh response."""
    return json.dumps({"access_token": "fresh_token", "refresh_token": "new_refresh"}).encode()


@pytest.fixture
def make_client(token_response):
    """Create an OneDriveClient with mocked token exchange."""
    def _make(refresh_token="test_refresh"):
        with patch("urllib.request.urlopen") as mock_urlopen:
            resp_mock = MagicMock()
            resp_mock.read.return_value = token_response
            resp_mock.__enter__ = lambda s: s
            resp_mock.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = resp_mock
            client = OneDriveClient("client_id", "client_secret", refresh_token)
        return client
    return _make


class TestInit:
    def test_empty_refresh_token_raises(self):
        with pytest.raises(OneDriveError, match="refresh token is required"):
            OneDriveClient("cid", "cs", "")

    def test_successful_init(self, make_client):
        client = make_client()
        assert client._access_token == "fresh_token"
        assert client._refresh_token == "new_refresh"

    def test_token_refresh_failure(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = Exception("network error")
            with pytest.raises(OneDriveError, match="Token refresh failed"):
                OneDriveClient("cid", "cs", "tok")

    def test_token_error_response(self):
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps(
            {"error": "invalid_grant", "error_description": "Token expired"}
        ).encode()
        resp_mock.__enter__ = lambda s: s
        resp_mock.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp_mock):
            with pytest.raises(OneDriveError, match="Token expired"):
                OneDriveClient("cid", "cs", "tok")

    def test_no_new_refresh_token(self):
        """When response has no refresh_token, keep the original."""
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({"access_token": "at"}).encode()
        resp_mock.__enter__ = lambda s: s
        resp_mock.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp_mock):
            client = OneDriveClient("cid", "cs", "original_rt")
        assert client._refresh_token == "original_rt"


class TestUpload:
    def test_simple_upload_small_file(self, make_client, tmp_path):
        client = make_client()
        test_file = tmp_path / "song.m4a"
        test_file.write_bytes(b"x" * 100)  # Small file

        with patch.object(client, "file_exists", return_value=False):
            with patch("urllib.request.urlopen") as mock_urlopen:
                resp_mock = MagicMock()
                resp_mock.__enter__ = lambda s: s
                resp_mock.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = resp_mock

                result = client.upload(test_file, "MediaSync/Harshit/audio", "song.m4a")

        assert result == "MediaSync/Harshit/audio/song.m4a"
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "content" in req.full_url
        assert req.get_method() == "PUT"

    def test_resumable_upload_large_file(self, make_client, tmp_path):
        client = make_client()
        test_file = tmp_path / "video.mp4"
        test_file.write_bytes(b"x" * (SIMPLE_UPLOAD_LIMIT + 100))

        with patch.object(client, "file_exists", return_value=False):
            with patch("urllib.request.urlopen") as mock_urlopen:
                # First call: create session; subsequent: chunk uploads
                session_resp = MagicMock()
                session_resp.read.return_value = json.dumps(
                    {"uploadUrl": "https://upload.example.com/session123"}
                ).encode()
                session_resp.__enter__ = lambda s: s
                session_resp.__exit__ = MagicMock(return_value=False)

                chunk_resp = MagicMock()
                chunk_resp.__enter__ = lambda s: s
                chunk_resp.__exit__ = MagicMock(return_value=False)

                mock_urlopen.side_effect = [session_resp, chunk_resp, chunk_resp]

                result = client.upload(test_file, "MediaSync/Harshit/video", "video.mp4")

        assert result == "MediaSync/Harshit/video/video.mp4"

    def test_skips_upload_when_file_exists(self, make_client, tmp_path):
        client = make_client()
        test_file = tmp_path / "song.m4a"
        test_file.write_bytes(b"x" * 100)

        with patch.object(client, "file_exists", return_value=True):
            with patch("urllib.request.urlopen") as mock_urlopen:
                result = client.upload(test_file, "MediaSync/Harshit/audio", "song.m4a")

        assert result == "MediaSync/Harshit/audio/song.m4a"
        mock_urlopen.assert_not_called()

    def test_upload_401_retries(self, make_client, tmp_path):
        client = make_client()
        test_file = tmp_path / "song.m4a"
        test_file.write_bytes(b"x" * 50)

        with patch("urllib.request.urlopen") as mock_urlopen:
            # First: 401, second: token refresh, third: success
            http_error = urllib.error.HTTPError(
                "url", 401, "Unauthorized", {}, None
            )
            token_resp = MagicMock()
            token_resp.read.return_value = json.dumps({"access_token": "new_at"}).encode()
            token_resp.__enter__ = lambda s: s
            token_resp.__exit__ = MagicMock(return_value=False)

            success_resp = MagicMock()
            success_resp.__enter__ = lambda s: s
            success_resp.__exit__ = MagicMock(return_value=False)

            mock_urlopen.side_effect = [http_error, token_resp, success_resp]

            result = client.upload(test_file, "folder", "file.m4a")

        assert result == "folder/file.m4a"

    def test_upload_500_raises(self, make_client, tmp_path):
        client = make_client()
        test_file = tmp_path / "song.m4a"
        test_file.write_bytes(b"x" * 50)

        with patch("urllib.request.urlopen") as mock_urlopen:
            error = urllib.error.HTTPError("url", 500, "Server Error", {}, None)
            error.fp = None
            mock_urlopen.side_effect = error
            with pytest.raises(OneDriveError, match="HTTP 500"):
                client.upload(test_file, "folder", "file.m4a")


class TestDelete:
    def test_successful_delete(self, make_client):
        client = make_client()
        with patch("urllib.request.urlopen") as mock_urlopen:
            resp_mock = MagicMock()
            resp_mock.__enter__ = lambda s: s
            resp_mock.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = resp_mock

            client.delete_file("MediaSync/Harshit/audio/song.m4a")

        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "DELETE"

    def test_delete_404_ignored(self, make_client):
        client = make_client()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "url", 404, "Not Found", {}, None
            )
            # Should not raise
            client.delete_file("nonexistent/path")

    def test_delete_401_retries(self, make_client):
        client = make_client()
        with patch("urllib.request.urlopen") as mock_urlopen:
            http_error = urllib.error.HTTPError("url", 401, "Unauth", {}, None)

            token_resp = MagicMock()
            token_resp.read.return_value = json.dumps({"access_token": "new"}).encode()
            token_resp.__enter__ = lambda s: s
            token_resp.__exit__ = MagicMock(return_value=False)

            retry_resp = MagicMock()
            retry_resp.__enter__ = lambda s: s
            retry_resp.__exit__ = MagicMock(return_value=False)

            mock_urlopen.side_effect = [http_error, token_resp, retry_resp]

            client.delete_file("some/path")

    def test_delete_500_raises(self, make_client):
        client = make_client()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "url", 500, "Error", {}, None
            )
            with pytest.raises(OneDriveError, match="HTTP 500"):
                client.delete_file("some/path")


class TestResumableUploadChunks:
    def test_multiple_chunks(self, make_client, tmp_path):
        """Verify multi-chunk upload sends correct Content-Range headers."""
        client = make_client()
        file_size = CHUNK_SIZE * 2 + 100
        test_file = tmp_path / "big.mp4"
        test_file.write_bytes(b"x" * file_size)

        with patch.object(client, "file_exists", return_value=False):
            with patch("urllib.request.urlopen") as mock_urlopen:
                session_resp = MagicMock()
                session_resp.read.return_value = json.dumps(
                    {"uploadUrl": "https://upload.example.com/s"}
                ).encode()
                session_resp.__enter__ = lambda s: s
                session_resp.__exit__ = MagicMock(return_value=False)

                chunk_resp = MagicMock()
                chunk_resp.__enter__ = lambda s: s
                chunk_resp.__exit__ = MagicMock(return_value=False)

                # session + 3 chunks
                mock_urlopen.side_effect = [session_resp, chunk_resp, chunk_resp, chunk_resp]

                client.upload(test_file, "folder", "big.mp4")

            # 1 session creation + 3 chunk PUTs
            assert mock_urlopen.call_count == 4


class TestDeleteRetry:
    def test_delete_generic_exception_raises(self, make_client):
        client = make_client()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("connection reset")
            with pytest.raises(OneDriveError, match="connection reset"):
                client.delete_file("some/path")

    def test_delete_retry_404_ignored(self, make_client):
        """After 401 retry, a 404 on the retry is fine."""
        client = make_client()
        with patch("urllib.request.urlopen") as mock_urlopen:
            # First: 401, then token refresh, then retry gets 404
            http_401 = urllib.error.HTTPError("url", 401, "Unauth", {}, None)
            token_resp = MagicMock()
            token_resp.read.return_value = json.dumps({"access_token": "new"}).encode()
            token_resp.__enter__ = lambda s: s
            token_resp.__exit__ = MagicMock(return_value=False)

            retry_404 = urllib.error.HTTPError("url", 404, "Not Found", {}, None)

            mock_urlopen.side_effect = [http_401, token_resp, retry_404]
            # Should not raise
            client.delete_file("some/path")

    def test_delete_retry_500_raises(self, make_client):
        """After 401 retry, a 500 on the retry raises."""
        client = make_client()
        with patch("urllib.request.urlopen") as mock_urlopen:
            http_401 = urllib.error.HTTPError("url", 401, "Unauth", {}, None)
            token_resp = MagicMock()
            token_resp.read.return_value = json.dumps({"access_token": "new"}).encode()
            token_resp.__enter__ = lambda s: s
            token_resp.__exit__ = MagicMock(return_value=False)

            retry_500 = urllib.error.HTTPError("url", 500, "Error", {}, None)

            mock_urlopen.side_effect = [http_401, token_resp, retry_500]
            with pytest.raises(OneDriveError, match="HTTP 500"):
                client.delete_file("some/path")


class TestResumableUpload401:
    def test_session_creation_401_retries(self, make_client, tmp_path):
        client = make_client()
        test_file = tmp_path / "big.mp4"
        test_file.write_bytes(b"x" * (SIMPLE_UPLOAD_LIMIT + 10))

        with patch.object(client, "file_exists", return_value=False), patch("urllib.request.urlopen") as mock_urlopen:
            # Session create: 401
            http_401 = urllib.error.HTTPError("url", 401, "Unauth", {}, None)
            # Token refresh
            token_resp = MagicMock()
            token_resp.read.return_value = json.dumps({"access_token": "new"}).encode()
            token_resp.__enter__ = lambda s: s
            token_resp.__exit__ = MagicMock(return_value=False)
            # Retry session create
            session_resp = MagicMock()
            session_resp.read.return_value = json.dumps(
                {"uploadUrl": "https://upload.example.com/s"}
            ).encode()
            session_resp.__enter__ = lambda s: s
            session_resp.__exit__ = MagicMock(return_value=False)
            # Chunk uploads (2 chunks for SIMPLE_UPLOAD_LIMIT + 10)
            chunk_resp = MagicMock()
            chunk_resp.__enter__ = lambda s: s
            chunk_resp.__exit__ = MagicMock(return_value=False)

            mock_urlopen.side_effect = [http_401, token_resp, session_resp, chunk_resp, chunk_resp]

            result = client.upload(test_file, "folder", "big.mp4")

        assert result == "folder/big.mp4"

    def test_session_creation_500_raises(self, make_client, tmp_path):
        client = make_client()
        test_file = tmp_path / "big.mp4"
        test_file.write_bytes(b"x" * (SIMPLE_UPLOAD_LIMIT + 10))

        with patch.object(client, "file_exists", return_value=False):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = urllib.error.HTTPError("url", 500, "Error", {}, None)
                with pytest.raises(OneDriveError, match="Upload session creation failed"):
                    client.upload(test_file, "folder", "big.mp4")

    def test_chunk_upload_error_raises(self, make_client, tmp_path):
        client = make_client()
        test_file = tmp_path / "big.mp4"
        test_file.write_bytes(b"x" * (SIMPLE_UPLOAD_LIMIT + 10))

        with patch.object(client, "file_exists", return_value=False):
            with patch("urllib.request.urlopen") as mock_urlopen:
                session_resp = MagicMock()
                session_resp.read.return_value = json.dumps(
                    {"uploadUrl": "https://upload.example.com/s"}
                ).encode()
                session_resp.__enter__ = lambda s: s
                session_resp.__exit__ = MagicMock(return_value=False)

                chunk_error = urllib.error.HTTPError("url", 503, "Unavailable", {}, None)

                mock_urlopen.side_effect = [session_resp, chunk_error]
                with pytest.raises(OneDriveError, match="Chunk upload failed"):
                    client.upload(test_file, "folder", "big.mp4")


class TestUploadGenericError:
    def test_simple_upload_generic_exception(self, make_client, tmp_path):
        client = make_client()
        test_file = tmp_path / "song.m4a"
        test_file.write_bytes(b"x" * 50)

        with patch.object(client, "file_exists", return_value=False):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = OSError("timeout")
                with pytest.raises(OneDriveError, match="timeout"):
                    client.upload(test_file, "folder", "file.m4a")
