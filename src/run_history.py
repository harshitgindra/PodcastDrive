"""Write run metadata to S3 as an append-only JSONL file.

Each run appends one JSON line to ``_meta/runs.jsonl`` in the configured S3 bucket.
"""

import json
import logging
import os
import platform
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)

HISTORY_KEY = "_meta/runs.jsonl"


def record_run_start() -> dict:
    """Create a run record dict (call at start of run)."""
    return {
        "run_id": f"{int(time.time())}_{os.getpid()}",
        "runner": os.environ.get("RUNNER", "unknown"),
        "hostname": platform.node(),
        "trigger": os.environ.get("TRIGGER", "manual"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "duration_secs": None,
        "status": "running",
        "podcasts_processed": 0,
        "episodes_downloaded": 0,
        "errors": 0,
    }


def record_run_end(
    record: dict,
    *,
    status: str = "success",
    podcasts_processed: int = 0,
    episodes_downloaded: int = 0,
    errors: int = 0,
) -> dict:
    """Finalize a run record with results."""
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    record["status"] = status
    record["podcasts_processed"] = podcasts_processed
    record["episodes_downloaded"] = episodes_downloaded
    record["errors"] = errors

    # Calculate duration
    try:
        start = datetime.fromisoformat(record["started_at"])
        end = datetime.fromisoformat(record["finished_at"])
        record["duration_secs"] = int((end - start).total_seconds())
    except (ValueError, TypeError):
        pass

    return record


def save_run_history(record: dict) -> None:
    """Append a run record to S3 JSONL file."""
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        logger.debug("S3_BUCKET not set, skipping run history write")
        return

    try:
        s3 = boto3.client("s3")
        region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

        # Read existing file (if any)
        existing = ""
        try:
            resp = s3.get_object(Bucket=bucket, Key=HISTORY_KEY)
            existing = resp["Body"].read().decode("utf-8")
        except s3.exceptions.NoSuchKey:
            pass
        except Exception:
            # File might not exist yet
            pass

        # Append new line
        new_line = json.dumps(record, separators=(",", ":"))
        updated = existing.rstrip("\n") + "\n" + new_line + "\n" if existing else new_line + "\n"

        s3.put_object(
            Bucket=bucket,
            Key=HISTORY_KEY,
            Body=updated.encode("utf-8"),
            ContentType="application/x-ndjson",
        )
        logger.info("Run history saved to s3://%s/%s", bucket, HISTORY_KEY)

    except Exception as exc:
        logger.warning("Failed to save run history: %s", exc)
