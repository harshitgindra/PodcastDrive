"""Distributed lock using S3 for cross-machine coordination.

Uses a lock object in S3 with a TTL (max age). If the lock is held by
another runner and hasn't expired, the current run is skipped.

Lock is always released on completion (success or failure) via context manager.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

LOCK_KEY = "_meta/run.lock"
DEFAULT_TTL_SECONDS = 3600  # 1 hour — max expected run duration


class LockAcquireError(Exception):
    """Raised when the lock cannot be acquired."""
    pass


class S3Lock:
    """Distributed lock backed by an S3 object.

    Usage::

        lock = S3Lock(bucket="my-bucket")
        with lock:
            # do work
            ...

    If another runner holds the lock and it hasn't expired, raises
    :class:`LockAcquireError`.
    """

    def __init__(
        self,
        bucket: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        lock_key: str = LOCK_KEY,
    ):
        self.bucket = bucket or os.environ.get("S3_BUCKET", "")
        self.ttl_seconds = ttl_seconds
        self.lock_key = lock_key
        self.runner = os.environ.get("RUNNER", "unknown")
        self._s3 = boto3.client("s3")
        self._acquired = False

    def _read_lock(self) -> dict | None:
        """Read the current lock object from S3. Returns None if not found."""
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=self.lock_key)
            return json.loads(resp["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        except (json.JSONDecodeError, KeyError):
            return None

    def _write_lock(self) -> None:
        """Write a lock object to S3."""
        lock_data = {
            "runner": self.runner,
            "pid": os.getpid(),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": self.ttl_seconds,
        }
        self._s3.put_object(
            Bucket=self.bucket,
            Key=self.lock_key,
            Body=json.dumps(lock_data, indent=2).encode("utf-8"),
            ContentType="application/json",
        )

    def _delete_lock(self) -> None:
        """Remove the lock object from S3."""
        try:
            self._s3.delete_object(Bucket=self.bucket, Key=self.lock_key)
        except Exception as exc:
            logger.warning("Failed to delete lock: %s", exc)

    def _is_expired(self, lock_data: dict) -> bool:
        """Check if a lock has exceeded its TTL."""
        try:
            acquired_at = datetime.fromisoformat(lock_data["acquired_at"])
            ttl = lock_data.get("ttl_seconds", DEFAULT_TTL_SECONDS)
            elapsed = (datetime.now(timezone.utc) - acquired_at).total_seconds()
            return elapsed > ttl
        except (ValueError, KeyError, TypeError):
            return True  # Malformed lock — treat as expired

    def acquire(self) -> None:
        """Attempt to acquire the lock. Raises LockAcquireError if held."""
        if not self.bucket:
            logger.debug("S3_BUCKET not set, skipping distributed lock")
            self._acquired = True
            return

        existing = self._read_lock()

        if existing is not None:
            if self._is_expired(existing):
                logger.warning(
                    "Stale lock found (held by %s since %s, TTL %ds expired) — overriding",
                    existing.get("runner", "?"),
                    existing.get("acquired_at", "?"),
                    existing.get("ttl_seconds", 0),
                )
            else:
                raise LockAcquireError(
                    f"Lock held by '{existing.get('runner', '?')}' "
                    f"since {existing.get('acquired_at', '?')} "
                    f"(TTL {existing.get('ttl_seconds', 0)}s). "
                    f"Skipping this run."
                )

        self._write_lock()
        self._acquired = True
        logger.info("Distributed lock acquired by %s", self.runner)

    def release(self) -> None:
        """Release the lock if held by this runner. Always safe to call."""
        if not self.bucket:
            return
        # Verify we own the lock before deleting (prevents releasing another runner's lock)
        existing = self._read_lock()
        if existing and existing.get("runner") == self.runner and existing.get("pid") == os.getpid():
            self._delete_lock()
            self._acquired = False
            logger.info("Distributed lock released by %s", self.runner)
        elif self._acquired:
            # We thought we had it but lock was overridden (TTL expiry) — just clear state
            self._acquired = False
            logger.warning("Lock was overridden by another runner — nothing to release")

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False  # Don't suppress exceptions
