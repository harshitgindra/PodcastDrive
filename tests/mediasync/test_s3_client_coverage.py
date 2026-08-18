"""Additional S3 client tests to cover list_folder (lines 60-73)."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from mediasync.s3_client import S3Client


@pytest.fixture
def client():
    """S3Client with mocked boto3."""
    with patch("mediasync.s3_client.boto3") as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.client.return_value = mock_s3
        c = S3Client("my-bucket", "us-west-2")
        c._client = mock_s3
        yield c


class TestListFolder:
    def test_returns_filenames(self, client):
        paginator = MagicMock()
        client._client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "prefix/folder/song1.m4a"},
                    {"Key": "prefix/folder/song2.mp3"},
                    {"Key": "prefix/folder/subfolder/nested.m4a"},  # nested, skipped
                ]
            }
        ]

        result = client.list_folder("prefix/folder")
        assert result == {"song1.m4a", "song2.mp3"}

    def test_empty_folder(self, client):
        paginator = MagicMock()
        client._client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{"Contents": []}]

        result = client.list_folder("prefix/empty")
        assert result == set()

    def test_no_contents_key(self, client):
        paginator = MagicMock()
        client._client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{}]  # No 'Contents' key

        result = client.list_folder("prefix/missing")
        assert result == set()

    def test_client_error_returns_empty(self, client):
        paginator = MagicMock()
        client._client.get_paginator.return_value = paginator
        paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}},
            "ListObjectsV2",
        )

        result = client.list_folder("prefix/denied")
        assert result == set()

    def test_multiple_pages(self, client):
        paginator = MagicMock()
        client._client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "pfx/dir/a.m4a"}]},
            {"Contents": [{"Key": "pfx/dir/b.m4a"}]},
        ]

        result = client.list_folder("pfx/dir")
        assert result == {"a.m4a", "b.m4a"}

    def test_trailing_slash_handling(self, client):
        """Folder with trailing slash should work the same."""
        paginator = MagicMock()
        client._client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "folder/file.mp3"}]}
        ]

        result = client.list_folder("folder/")
        assert result == {"file.mp3"}
