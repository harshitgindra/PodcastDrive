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
    s3_bucket: str
    s3_region: str
    s3_prefix: str
    profiles: list[Profile]
    max_duration_secs: int = 7200
    output_dir: str = "/tmp/mediasync"
    herald_enabled: bool = True
    herald_job_id: str = "mediasync"

    @classmethod
    def from_env(cls) -> Config:
        """Load configuration from environment variables.

        Required:
            MEDIASYNC_NOTION_TOKEN
            MEDIASYNC_NOTION_DATABASE_ID
            MEDIASYNC_S3_BUCKET
            MEDIASYNC_PROFILES (comma-separated profile names)

        Optional:
            MEDIASYNC_S3_REGION (default: us-west-2)
            MEDIASYNC_S3_PREFIX (default: MediaSync)
            MEDIASYNC_MAX_DURATION_SECS (default: 7200)
            MEDIASYNC_OUTPUT_DIR (default: /tmp/mediasync)
            MEDIASYNC_HERALD_ENABLED (default: true)
            MEDIASYNC_HERALD_JOB_ID (default: mediasync)
        """
        notion_token = os.environ.get("MEDIASYNC_NOTION_TOKEN", "")
        notion_db_id = os.environ.get("MEDIASYNC_NOTION_DATABASE_ID", "")
        s3_bucket = os.environ.get("MEDIASYNC_S3_BUCKET", "")
        profiles_raw = os.environ.get("MEDIASYNC_PROFILES", "")

        if not notion_token:
            raise ValueError("MEDIASYNC_NOTION_TOKEN is required")
        if not notion_db_id:
            raise ValueError("MEDIASYNC_NOTION_DATABASE_ID is required")
        if not s3_bucket:
            raise ValueError("MEDIASYNC_S3_BUCKET is required")
        if not profiles_raw:
            raise ValueError("MEDIASYNC_PROFILES is required (comma-separated)")

        profiles = [Profile(name=p.strip()) for p in profiles_raw.split(",") if p.strip()]

        return cls(
            notion_token=notion_token,
            notion_database_id=notion_db_id,
            s3_bucket=s3_bucket,
            s3_region=os.environ.get("MEDIASYNC_S3_REGION", "us-west-2"),
            s3_prefix=os.environ.get("MEDIASYNC_S3_PREFIX", "MediaSync"),
            profiles=profiles,
            max_duration_secs=int(os.environ.get("MEDIASYNC_MAX_DURATION_SECS", "7200")),
            output_dir=os.environ.get("MEDIASYNC_OUTPUT_DIR", "/tmp/mediasync"),
            herald_enabled=os.environ.get("MEDIASYNC_HERALD_ENABLED", "true").lower() == "true",
            herald_job_id=os.environ.get("MEDIASYNC_HERALD_JOB_ID", "mediasync"),
        )
