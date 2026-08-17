"""Storage backend abstraction for MediaSync.

Provides a common interface over S3 and OneDrive so the pipeline
does not need to know which backend is in use.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when a storage operation fails."""


class StorageBackend(Protocol):
    """Protocol for storage backends (S3 or OneDrive)."""

    def file_exists(self, key: str) -> bool:
        """Check if a remote file exists."""
        ...

    def upload(self, local_path: Path, remote_folder: str, filename: str) -> str:
        """Upload a file. Returns the remote key/path. Skips if already exists."""
        ...

    def delete_file(self, key: str) -> None:
        """Delete a remote file. Idempotent (ignores missing)."""
        ...


def create_storage(config) -> StorageBackend:
    """Factory: build the configured storage backend.

    Args:
        config: mediasync.config.Config instance.

    Returns:
        A StorageBackend implementation (S3Client or OneDriveClient).
    """

    if config.storage_backend == "onedrive":
        from mediasync.onedrive_client import OneDriveClient

        return OneDriveClient(
            client_id=config.onedrive_client_id,
            client_secret=config.onedrive_client_secret,
            refresh_token=config.onedrive_refresh_token,
        )
    else:
        from mediasync.s3_client import S3Client

        return S3Client(config.s3_bucket, config.s3_region)
