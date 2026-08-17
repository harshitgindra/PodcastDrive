"""S3 client for MediaSync — upload and delete media files.

Uses boto3 with default credential chain (env vars, ~/.aws, instance profile).
"""

from __future__ import annotations

import logging
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class S3Error(Exception):
    """Raised when an S3 operation fails."""


class S3Client:
    """Client for uploading and managing media files on S3."""

    def __init__(self, bucket: str, region: str = "us-west-2") -> None:
        """Initialize S3 client.

        Args:
            bucket: S3 bucket name.
            region: AWS region (default: us-west-2).
        """
        if not bucket:
            raise S3Error("S3 bucket name is required")
        self._bucket = bucket
        self._client = boto3.client("s3", region_name=region)

    def file_exists(self, key: str) -> bool:
        """Check if a file already exists in S3.

        Args:
            key: Full S3 key to check.

        Returns:
            True if the object exists, False otherwise.
        """
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "404":
                return False
            return False

    def upload(self, local_path: Path, remote_folder: str, filename: str) -> str:
        """Upload a file to S3.

        Skips upload if a file with the same key already exists (idempotent).

        Args:
            local_path: Path to the local file.
            remote_folder: S3 key prefix (e.g., "Harshit/audio").
            filename: Filename to use as the S3 key suffix.

        Returns:
            Full S3 key of the uploaded file.
        """
        key = f"{remote_folder}/{filename}"

        if self.file_exists(key):
            logger.info("Already exists in S3, skipping: s3://%s/%s", self._bucket, key)
            return key

        content_type = self._guess_content_type(filename)

        try:
            self._client.upload_file(
                str(local_path),
                self._bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        except ClientError as exc:
            raise S3Error(f"Upload failed for {key}: {exc}") from exc

        logger.info("Uploaded s3://%s/%s (%d bytes)", self._bucket, key, local_path.stat().st_size)
        return key

    def delete_file(self, key: str) -> None:
        """Delete a file from S3. Idempotent (no error if key doesn't exist)."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise S3Error(f"Delete failed for {key}: {exc}") from exc
        logger.info("Deleted s3://%s/%s", self._bucket, key)

    @staticmethod
    def _guess_content_type(filename: str) -> str:
        """Map file extension to MIME type."""
        ext = Path(filename).suffix.lower()
        return {
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".opus": "audio/opus",
        }.get(ext, "application/octet-stream")
