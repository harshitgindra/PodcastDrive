"""Upload run logs to S3 for centralized cross-machine analysis.

After each run, uploads the current run's log as a JSON file to:
    s3://<bucket>/_meta/logs/<date>/<runner>_<timestamp>.jsonl

The log file uses JSON Lines format — one JSON object per log line,
making it easy to parse, filter, and aggregate across runs.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import boto3

logger = logging.getLogger(__name__)

LOGS_PREFIX = "_meta/logs"

# Pattern to parse the standard text log format
_TEXT_LOG_PATTERN = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+\[(?P<level>\w+)\s*\]\s+\[(?P<logger>[^\]]+)\]\s+\[(?P<runner>[^\]]*)\]\s+(?P<message>.*)$"
)


def _parse_text_log_line(line: str) -> dict | None:
    """Parse a text log line into a structured dict."""
    m = _TEXT_LOG_PATTERN.match(line.strip())
    if not m:
        return None
    return {
        "timestamp": m.group("timestamp"),
        "level": m.group("level").strip(),
        "logger": m.group("logger"),
        "runner": m.group("runner"),
        "message": m.group("message"),
    }


def upload_run_log(
    log_dir: str | None = None,
    run_id: str | None = None,
) -> str | None:
    """Upload the current run's log file to S3.

    Reads the local log file, converts to JSON Lines if needed,
    and uploads to the centralized S3 log store.

    Args:
        log_dir: Directory containing log files. Defaults to LOG_DIR env var.
        run_id: Optional run identifier for the S3 key.

    Returns:
        S3 key of the uploaded log, or None on failure.
    """
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        return None

    log_dir = log_dir or os.environ.get("LOG_DIR", "./logs")
    log_file = Path(log_dir) / "playlist_downloader.log"

    if not log_file.exists():
        logger.debug("No log file found at %s", log_file)
        return None

    runner = os.environ.get("RUNNER", "unknown")
    now = datetime.now(timezone.utc)
    date_prefix = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%H%M%S")

    # Sanitize runner for S3 key (replace / with _)
    safe_runner = runner.replace("/", "_").replace(" ", "-")
    s3_key = f"{LOGS_PREFIX}/{date_prefix}/{safe_runner}_{timestamp}.jsonl"

    try:
        # Read log file
        content = log_file.read_text(encoding="utf-8", errors="replace")
        lines = content.strip().split("\n")

        # Convert to JSON Lines
        jsonl_lines = []
        for line in lines:
            if not line.strip():
                continue

            # Try parsing as JSON first (if LOG_FORMAT=json was used)
            try:
                parsed = json.loads(line)
                jsonl_lines.append(json.dumps(parsed, separators=(",", ":")))
                continue
            except json.JSONDecodeError:
                pass

            # Parse text format
            parsed = _parse_text_log_line(line)
            if parsed:
                jsonl_lines.append(json.dumps(parsed, separators=(",", ":")))
            else:
                # Unparseable line — include as raw message
                jsonl_lines.append(json.dumps({
                    "level": "RAW",
                    "message": line.strip(),
                    "runner": runner,
                }, separators=(",", ":")))

        if not jsonl_lines:
            logger.debug("Log file is empty, nothing to upload")
            return None

        # Add run metadata header
        header = json.dumps({
            "type": "run_header",
            "runner": runner,
            "trigger": os.environ.get("TRIGGER", "manual"),
            "date": date_prefix,
            "timestamp": now.isoformat(),
            "total_lines": len(jsonl_lines),
            "errors": sum(1 for l in jsonl_lines if '"ERROR"' in l),
            "warnings": sum(1 for l in jsonl_lines if '"WARNING"' in l),
        }, separators=(",", ":"))

        body = header + "\n" + "\n".join(jsonl_lines) + "\n"

        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=body.encode("utf-8"),
            ContentType="application/x-ndjson",
        )
        logger.info("Run log uploaded to s3://%s/%s (%d lines)", bucket, s3_key, len(jsonl_lines))
        return s3_key

    except Exception as exc:
        logger.warning("Failed to upload run log: %s", exc)
        return None
