"""Tests for mediasync.storage."""

from __future__ import annotations

from unittest.mock import patch

from mediasync.config import Config, Profile
from mediasync.storage import create_storage


def _base_config(**overrides):
    """Create a Config with defaults, applying overrides."""
    defaults = dict(
        notion_token="tok",
        notion_database_id="dbid",
        profiles=[Profile("Harshit")],
        storage_backend="s3",
        s3_bucket="bucket",
        s3_region="us-west-2",
        s3_prefix="MediaSync",
        onedrive_client_id="",
        onedrive_client_secret="",
        onedrive_refresh_token="",
        onedrive_prefix="MediaSync",
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestCreateStorage:
    def test_s3_backend(self):
        config = _base_config(storage_backend="s3", s3_bucket="mybucket")
        with patch("mediasync.s3_client.S3Client") as mock_s3:
            create_storage(config)
        mock_s3.assert_called_once_with("mybucket", "us-west-2")

    def test_onedrive_backend(self):
        config = _base_config(
            storage_backend="onedrive",
            onedrive_client_id="cid",
            onedrive_client_secret="cs",
            onedrive_refresh_token="rt",
        )
        with patch("mediasync.onedrive_client.OneDriveClient") as mock_od:
            create_storage(config)
        mock_od.assert_called_once_with(
            client_id="cid", client_secret="cs", refresh_token="rt"
        )
