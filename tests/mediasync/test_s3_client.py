"""Tests for mediasync.s3_client module."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from botocore.exceptions import ClientError

from mediasync.s3_client import S3Client, S3Error


@pytest.fixture
def client():
    """S3Client with mocked boto3."""
    with patch("mediasync.s3_client.boto3") as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.client.return_value = mock_s3
        c = S3Client("hg-mediafiles", "us-west-2")
        c._client = mock_s3
        yield c


class TestInit:
    def test_empty_bucket_raises(self):
        with pytest.raises(S3Error, match="bucket name is required"):
            S3Client("")

    def test_creates_client_with_region(self):
        with patch("mediasync.s3_client.boto3") as mock_boto:
            S3Client("my-bucket", "eu-west-1")
            mock_boto.client.assert_called_once_with("s3", region_name="eu-west-1")


class TestUpload:
    def test_successful_upload(self, client, tmp_path):
        test_file = tmp_path / "song.m4a"
        test_file.write_bytes(b"fake audio data")

        key = client.upload(test_file, "MediaSync/Harshit/audio", "song.m4a")

        assert key == "MediaSync/Harshit/audio/song.m4a"
        client._client.upload_file.assert_called_once_with(
            str(test_file),
            "hg-mediafiles",
            "MediaSync/Harshit/audio/song.m4a",
            ExtraArgs={"ContentType": "audio/mp4"},
        )

    def test_upload_mp4_content_type(self, client, tmp_path):
        test_file = tmp_path / "video.mp4"
        test_file.write_bytes(b"fake video")

        client.upload(test_file, "MediaSync/Harshit/video", "video.mp4")

        call_args = client._client.upload_file.call_args
        assert call_args[1]["ExtraArgs"]["ContentType"] == "video/mp4"

    def test_upload_mp3_content_type(self, client, tmp_path):
        test_file = tmp_path / "song.mp3"
        test_file.write_bytes(b"fake mp3")

        client.upload(test_file, "prefix", "song.mp3")

        call_args = client._client.upload_file.call_args
        assert call_args[1]["ExtraArgs"]["ContentType"] == "audio/mpeg"

    def test_upload_unknown_extension(self, client, tmp_path):
        test_file = tmp_path / "file.xyz"
        test_file.write_bytes(b"data")

        client.upload(test_file, "prefix", "file.xyz")

        call_args = client._client.upload_file.call_args
        assert call_args[1]["ExtraArgs"]["ContentType"] == "application/octet-stream"

    def test_upload_failure_raises(self, client, tmp_path):
        test_file = tmp_path / "song.m4a"
        test_file.write_bytes(b"data")

        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "Forbidden"}}, "PutObject")
        client._client.upload_file.side_effect = error

        with pytest.raises(S3Error, match="Upload failed"):
            client.upload(test_file, "prefix", "song.m4a")


class TestDeleteFile:
    def test_successful_delete(self, client):
        client.delete_file("MediaSync/Harshit/audio/song.m4a")

        client._client.delete_object.assert_called_once_with(
            Bucket="hg-mediafiles", Key="MediaSync/Harshit/audio/song.m4a"
        )

    def test_delete_failure_raises(self, client):
        error = ClientError({"Error": {"Code": "InternalError", "Message": "Server error"}}, "DeleteObject")
        client._client.delete_object.side_effect = error

        with pytest.raises(S3Error, match="Delete failed"):
            client.delete_file("some/key.m4a")


class TestGuessContentType:
    def test_m4a(self):
        assert S3Client._guess_content_type("song.m4a") == "audio/mp4"

    def test_mp3(self):
        assert S3Client._guess_content_type("song.mp3") == "audio/mpeg"

    def test_mp4(self):
        assert S3Client._guess_content_type("video.mp4") == "video/mp4"

    def test_webm(self):
        assert S3Client._guess_content_type("video.webm") == "video/webm"

    def test_opus(self):
        assert S3Client._guess_content_type("audio.opus") == "audio/opus"

    def test_unknown(self):
        assert S3Client._guess_content_type("file.bin") == "application/octet-stream"
