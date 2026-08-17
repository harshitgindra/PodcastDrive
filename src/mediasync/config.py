"""MediaSync configuration loading.

Config is read from environment variables (for portability across machines).
All settings have MEDIASYNC_ prefix.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    name: str


@dataclass(frozen=True)
class Config:
    notion_token: str
    notion_database_id: str
    profiles: list[Profile]
    storage_backend: str = "s3"  # "s3" or "onedrive"
    # S3 settings (required when storage_backend=s3)
    s3_bucket: str = ""
    s3_region: str = "us-west-2"
    s3_prefix: str = "MediaSync"
    # OneDrive settings (required when storage_backend=onedrive)
    onedrive_client_id: str = ""
    onedrive_client_secret: str = ""
    onedrive_refresh_token: str = ""
    onedrive_prefix: str = "MediaSync"
    # General
    max_duration_secs: int = 7200
    output_dir: str = "/tmp/mediasync"
    herald_enabled: bool = True
    # The job id the Herald listener injects; empty means "send to the
    # configured default chat". Never invent a token: routing looks it up in
    # jobs.json and refuses to deliver to an id that is not there.
    herald_job_id: str = ""

    @classmethod
    def from_env(cls) -> Config:
        """Load configuration from environment variables.

        Required:
            MEDIASYNC_NOTION_TOKEN
            MEDIASYNC_NOTION_DATABASE_ID
            MEDIASYNC_PROFILES (comma-separated profile names)
            MEDIASYNC_STORAGE (s3 or onedrive, default: s3)

        S3 backend:
            MEDIASYNC_S3_BUCKET (required)
            MEDIASYNC_S3_REGION (default: us-west-2)
            MEDIASYNC_S3_PREFIX (default: MediaSync)

        OneDrive backend:
            MEDIASYNC_ONEDRIVE_CLIENT_ID (required)
            MEDIASYNC_ONEDRIVE_CLIENT_SECRET (required)
            MEDIASYNC_ONEDRIVE_REFRESH_TOKEN (required)
            MEDIASYNC_ONEDRIVE_PREFIX (default: MediaSync)

        Optional:
            MEDIASYNC_MAX_DURATION_SECS (default: 7200)
            MEDIASYNC_OUTPUT_DIR (default: /tmp/mediasync)
            MEDIASYNC_HERALD_ENABLED (default: true)
            HERALD_JOB_ID (injected by the Herald listener; unset means
                          notifications go to the default chat)
        """
        notion_token = os.environ.get("MEDIASYNC_NOTION_TOKEN", "")
        notion_db_id = os.environ.get("MEDIASYNC_NOTION_DATABASE_ID", "")
        profiles_raw = os.environ.get("MEDIASYNC_PROFILES", "")
        storage = os.environ.get("MEDIASYNC_STORAGE", "s3").lower()

        if not notion_token:
            raise ValueError("MEDIASYNC_NOTION_TOKEN is required")
        if not notion_db_id:
            raise ValueError("MEDIASYNC_NOTION_DATABASE_ID is required")
        if not profiles_raw:
            raise ValueError("MEDIASYNC_PROFILES is required (comma-separated)")
        if storage not in ("s3", "onedrive"):
            raise ValueError("MEDIASYNC_STORAGE must be 's3' or 'onedrive'")

        profiles = [Profile(name=p.strip()) for p in profiles_raw.split(",") if p.strip()]

        # Backend-specific validation
        s3_bucket = os.environ.get("MEDIASYNC_S3_BUCKET", "")
        onedrive_client_id = os.environ.get("MEDIASYNC_ONEDRIVE_CLIENT_ID", "")
        onedrive_client_secret = os.environ.get("MEDIASYNC_ONEDRIVE_CLIENT_SECRET", "")
        onedrive_refresh_token = os.environ.get("MEDIASYNC_ONEDRIVE_REFRESH_TOKEN", "")

        if storage == "s3" and not s3_bucket:
            raise ValueError("MEDIASYNC_S3_BUCKET is required when MEDIASYNC_STORAGE=s3")
        if storage == "onedrive":
            if not onedrive_client_id:
                raise ValueError("MEDIASYNC_ONEDRIVE_CLIENT_ID is required when MEDIASYNC_STORAGE=onedrive")
            if not onedrive_client_secret:
                raise ValueError("MEDIASYNC_ONEDRIVE_CLIENT_SECRET is required when MEDIASYNC_STORAGE=onedrive")
            if not onedrive_refresh_token:
                raise ValueError("MEDIASYNC_ONEDRIVE_REFRESH_TOKEN is required when MEDIASYNC_STORAGE=onedrive")

        return cls(
            notion_token=notion_token,
            notion_database_id=notion_db_id,
            profiles=profiles,
            storage_backend=storage,
            s3_bucket=s3_bucket,
            s3_region=os.environ.get("MEDIASYNC_S3_REGION", "us-west-2"),
            s3_prefix=os.environ.get("MEDIASYNC_S3_PREFIX", "MediaSync"),
            onedrive_client_id=onedrive_client_id,
            onedrive_client_secret=onedrive_client_secret,
            onedrive_refresh_token=onedrive_refresh_token,
            onedrive_prefix=os.environ.get("MEDIASYNC_ONEDRIVE_PREFIX", "MediaSync"),
            max_duration_secs=int(os.environ.get("MEDIASYNC_MAX_DURATION_SECS", "7200")),
            output_dir=os.environ.get("MEDIASYNC_OUTPUT_DIR", "/tmp/mediasync"),
            herald_enabled=os.environ.get("MEDIASYNC_HERALD_ENABLED", "true").lower() == "true",
            herald_job_id=os.environ.get("HERALD_JOB_ID", "").strip(),
        )

    @property
    def prefix(self) -> str:
        """Return the active storage prefix based on backend."""
        if self.storage_backend == "onedrive":
            return self.onedrive_prefix
        return self.s3_prefix
