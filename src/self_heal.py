"""Self-healing: automated safe remediation for known failure patterns.

Tier 2 actions only — no code changes, just data operations:
- Retry queue: mark failed episodes for retry on next run
- Cache clear: auto-clear ad cache for repeatedly-failed episodes
- Manifest backfill: fill missing upload_date from yt-dlp metadata

Usage:
    python -m self_heal                     # run all healers
    python -m self_heal --action retry      # just process retry queue
    python -m self_heal --action cache      # just clear stale caches
    python -m self_heal --action backfill   # just backfill manifest dates
    python -m self_heal --dry-run           # preview without changes
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime

import boto3

logger = logging.getLogger(__name__)

RETRY_QUEUE_KEY = "_meta/retry_queue.json"
RUNS_KEY = "_meta/runs.jsonl"
LOGS_PREFIX = "_meta/logs"


def _load_retry_queue(s3, bucket: str) -> dict:
    """Load the retry queue from S3."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=RETRY_QUEUE_KEY)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except Exception:
        return {"episodes": {}}


def _save_retry_queue(s3, bucket: str, queue: dict) -> None:
    """Save the retry queue to S3."""
    s3.put_object(
        Bucket=bucket,
        Key=RETRY_QUEUE_KEY,
        Body=json.dumps(queue, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def _scan_logs_for_failures(s3, bucket: str, days: int = 3) -> dict:
    """Scan recent logs for episode failures, grouped by video_id."""
    import re
    from datetime import timedelta

    failures = defaultdict(list)  # video_id → [{"error": ..., "date": ..., "type": ...}]
    since_date = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{LOGS_PREFIX}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                parts = key.split("/")
                if len(parts) >= 4 and parts[2] >= since_date:
                    try:
                        resp = s3.get_object(Bucket=bucket, Key=key)
                        content = resp["Body"].read().decode("utf-8")
                        for line in content.split("\n"):
                            if not line.strip():
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if record.get("level") != "ERROR":
                                continue
                            msg = record.get("message", "")
                            # Extract video_id from error messages
                            # Pattern: "FAILED <video_id>:" or "failed for <video_id>:"
                            vid_match = re.search(r"(?:FAILED|failed for)\s+(\S+?)[\s:]", msg)
                            if vid_match:
                                vid = vid_match.group(1)
                                error_type = "unknown"
                                if "Splic" in msg:
                                    error_type = "splice"
                                elif "Transcri" in msg:
                                    error_type = "transcribe"
                                elif "detection" in msg.lower() or "detect" in msg.lower():
                                    error_type = "ad_detection"
                                elif "download" in msg.lower():
                                    error_type = "download"
                                failures[vid].append(
                                    {
                                        "error": msg[:200],
                                        "type": error_type,
                                        "date": parts[2],
                                    }
                                )
                    except Exception:
                        continue
    except Exception as exc:
        logger.warning("Failed to scan logs: %s", exc)

    return dict(failures)


def heal_retry_queue(s3, bucket: str, dry_run: bool = False) -> dict:
    """Update retry queue based on recent failures.

    Episodes that failed are added to the queue with retry_count.
    Episodes that succeeded (exist in S3) are removed from the queue.
    """
    failures = _scan_logs_for_failures(s3, bucket)
    queue = _load_retry_queue(s3, bucket)
    episodes = queue.get("episodes", {})

    added = 0
    removed = 0

    # Add new failures
    for vid, failure_list in failures.items():
        if vid not in episodes:
            episodes[vid] = {
                "video_id": vid,
                "first_failure": failure_list[0]["date"],
                "last_failure": failure_list[-1]["date"],
                "failure_count": len(failure_list),
                "error_types": list({f["type"] for f in failure_list}),
                "last_error": failure_list[-1]["error"],
            }
            added += 1
        else:
            # Update existing
            episodes[vid]["failure_count"] += len(failure_list)
            episodes[vid]["last_failure"] = failure_list[-1]["date"]
            episodes[vid]["last_error"] = failure_list[-1]["error"]

    # Remove episodes that now exist in S3 (succeeded on retry)
    to_remove = []
    for vid in list(episodes.keys()):
        # Check all playlists — we don't know which playlist this belongs to
        # Just check if any key matching this vid exists
        try:
            resp = s3.list_objects_v2(
                Bucket=bucket,
                Prefix="",
                MaxKeys=1000,
            )
            # Search for the episode file
            found = False
            for obj in resp.get("Contents", []):
                if f"/episodes/{vid}.mp3" in obj["Key"]:
                    found = True
                    break
            if found:
                to_remove.append(vid)
        except Exception:
            pass

    for vid in to_remove:
        del episodes[vid]
        removed += 1

    queue["episodes"] = episodes
    queue["updated_at"] = datetime.now(UTC).isoformat()

    if not dry_run:
        _save_retry_queue(s3, bucket, queue)

    return {
        "action": "retry_queue",
        "added": added,
        "removed": removed,
        "total_queued": len(episodes),
        "dry_run": dry_run,
    }


def heal_cache_clear(s3, bucket: str, dry_run: bool = False) -> dict:
    """Clear ad-segment cache for episodes that failed splice 2+ times."""
    queue = _load_retry_queue(s3, bucket)
    episodes = queue.get("episodes", {})

    cleared = 0
    for vid, info in episodes.items():
        if info.get("failure_count", 0) >= 2 and "splice" in info.get("error_types", []):
            # Find and delete the _ads.json cache for this video
            try:
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=""):
                    for obj in page.get("Contents", []):
                        if obj["Key"].endswith(f"/{vid}_ads.json"):
                            if not dry_run:
                                s3.delete_object(Bucket=bucket, Key=obj["Key"])
                            logger.info("Cleared ad cache: %s", obj["Key"])
                            cleared += 1
                            break
                    if cleared:
                        break
            except Exception as exc:
                logger.warning("Failed to clear cache for %s: %s", vid, exc)

    return {
        "action": "cache_clear",
        "cleared": cleared,
        "dry_run": dry_run,
    }


def heal_manifest_backfill(s3, bucket: str, dry_run: bool = False) -> dict:
    """Backfill missing upload_date in manifests from yt-dlp metadata."""
    backfilled = 0
    checked_playlists = 0

    try:
        # Find all manifest.json files
        paginator = s3.get_paginator("list_objects_v2")
        manifest_keys = []
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("/manifest.json") and not obj["Key"].startswith("_meta"):
                    manifest_keys.append(obj["Key"])

        for key in manifest_keys:
            checked_playlists += 1
            try:
                resp = s3.get_object(Bucket=bucket, Key=key)
                manifest = json.loads(resp["Body"].read().decode("utf-8"))
            except Exception:
                continue

            if not isinstance(manifest, dict):
                continue

            # Find entries missing upload_date
            missing = [vid for vid, data in manifest.items() if isinstance(data, dict) and not data.get("upload_date")]

            if not missing:
                continue

            logger.info("Manifest %s has %d entries missing upload_date", key, len(missing))

            # Try to get upload_date from yt-dlp (expensive — only do a few)
            for vid in missing[:5]:  # Limit to 5 per playlist per run
                try:
                    import yt_dlp

                    url = f"https://www.youtube.com/watch?v={vid}"
                    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
                        info = ydl.extract_info(url, download=False)
                    if info and info.get("upload_date"):
                        manifest[vid]["upload_date"] = info["upload_date"]
                        backfilled += 1
                        logger.info("Backfilled upload_date for %s: %s", vid, info["upload_date"])
                except Exception as exc:
                    logger.debug("Could not backfill %s: %s", vid, exc)

            if backfilled > 0 and not dry_run:
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=json.dumps(manifest, indent=2).encode("utf-8"),
                    ContentType="application/json",
                )

    except Exception as exc:
        logger.warning("Manifest backfill failed: %s", exc)

    return {
        "action": "manifest_backfill",
        "playlists_checked": checked_playlists,
        "backfilled": backfilled,
        "dry_run": dry_run,
    }


def run_all_healers(dry_run: bool = False) -> list[dict]:
    """Run all self-healing actions."""
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        logger.error("S3_BUCKET not set")
        return []

    s3 = boto3.client("s3")
    results = []

    logger.info("=== Self-Healing: retry queue ===")
    results.append(heal_retry_queue(s3, bucket, dry_run=dry_run))

    logger.info("=== Self-Healing: cache clear ===")
    results.append(heal_cache_clear(s3, bucket, dry_run=dry_run))

    logger.info("=== Self-Healing: manifest backfill ===")
    results.append(heal_manifest_backfill(s3, bucket, dry_run=dry_run))

    return results


def main():
    parser = argparse.ArgumentParser(description="PodcastDrive Self-Healing")
    parser.add_argument("--action", choices=["retry", "cache", "backfill", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        print("ERROR: S3_BUCKET not set", file=sys.stderr)
        sys.exit(1)

    s3 = boto3.client("s3")

    if args.action == "all":
        results = run_all_healers(dry_run=args.dry_run)
    elif args.action == "retry":
        results = [heal_retry_queue(s3, bucket, dry_run=args.dry_run)]
    elif args.action == "cache":
        results = [heal_cache_clear(s3, bucket, dry_run=args.dry_run)]
    elif args.action == "backfill":
        results = [heal_manifest_backfill(s3, bucket, dry_run=args.dry_run)]

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
