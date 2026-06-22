"""Write run metadata to S3 as an append-only JSONL file.

Each run writes its record to a unique key ``_meta/runs/<run_id>.json`` so
concurrent writers never conflict. The legacy ``_meta/runs.jsonl`` file is
still updated (best-effort) for backwards compatibility with health_report.
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
RUNS_PREFIX = "_meta/runs"


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
    """Write a run record to S3.

    Each record is written to its own key (``_meta/runs/<run_id>.json``) so
    concurrent runners never conflict. The legacy JSONL file is updated
    best-effort for backwards compatibility.
    """
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        logger.debug("S3_BUCKET not set, skipping run history write")
        return

    try:
        s3 = boto3.client("s3")
        run_id = record.get("run_id", f"{int(time.time())}_{os.getpid()}")
        record_json = json.dumps(record, separators=(",", ":"))

        # Write individual run record (atomic, no race)
        run_key = f"{RUNS_PREFIX}/{run_id}.json"
        s3.put_object(
            Bucket=bucket,
            Key=run_key,
            Body=record_json.encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("Run record saved to s3://%s/%s", bucket, run_key)

        # Best-effort append to legacy JSONL (race-tolerant: losing a line is acceptable)
        try:
            existing = ""
            try:
                resp = s3.get_object(Bucket=bucket, Key=HISTORY_KEY)
                existing = resp["Body"].read().decode("utf-8")
            except Exception:
                pass

            updated = existing.rstrip("\n") + "\n" + record_json + "\n" if existing else record_json + "\n"
            s3.put_object(
                Bucket=bucket,
                Key=HISTORY_KEY,
                Body=updated.encode("utf-8"),
                ContentType="application/x-ndjson",
            )
        except Exception as exc:
            logger.debug("Legacy JSONL append failed (non-critical): %s", exc)

    except Exception as exc:
        logger.warning("Failed to save run history: %s", exc)
