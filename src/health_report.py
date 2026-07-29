"""Health report generator — analyzes centralized logs from S3.

Reads run history and log files from S3, identifies failures and patterns,
and produces a structured report for human review.

Usage:
    python -m health_report                  # last 7 days
    python -m health_report --days 14        # last 14 days
    python -m health_report --output md      # markdown output
    python -m health_report --output json    # JSON output
"""

import argparse
import json
import logging
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

import boto3

logger = logging.getLogger(__name__)

LOGS_PREFIX = "_meta/logs"
RUNS_KEY = "_meta/runs.jsonl"


def _fetch_runs(s3, bucket: str, since: datetime) -> list[dict]:
    """Fetch run history entries since a given date.

    Deduplicates by run_id — later lines win — so the start-of-run
    "running" record is replaced by the final "success"/"error" record
    written at the end of each run.  Without this, every run contributes
    two JSONL entries and the health metrics (success rate, stuck runs)
    are wildly wrong.
    """
    try:
        resp = s3.get_object(Bucket=bucket, Key=RUNS_KEY)
        content = resp["Body"].read().decode("utf-8")
    except Exception:
        return []

    # First pass: deduplicate — the final status record overwrites the
    # start-of-run "running" record for the same run_id.
    seen: dict = {}
    for line in content.strip().split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            run_id = record.get("run_id")
            if run_id:
                seen[run_id] = record  # last occurrence wins (final status)
            else:
                seen[id(record)] = record  # no run_id: keep as-is
        except json.JSONDecodeError:
            continue

    # Second pass: filter by date
    runs = []
    for record in seen.values():
        started = record.get("started_at", "")
        if started:
            try:
                dt = datetime.fromisoformat(started)
                if dt >= since:
                    runs.append(record)
            except ValueError:
                continue
    return runs


def _fetch_log_files(s3, bucket: str, since: datetime) -> list[dict]:
    """Fetch all log files from S3 within the date range."""
    log_entries = []
    # List date prefixes
    since_date = since.strftime("%Y-%m-%d")

    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{LOGS_PREFIX}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # Extract date from key: _meta/logs/2026-06-22/runner_123456.jsonl
                parts = key.split("/")
                if len(parts) >= 4:
                    date_part = parts[2]
                    if date_part >= since_date:
                        log_entries.append({"key": key, "date": date_part})
    except Exception as exc:
        logger.warning("Failed to list log files: %s", exc)

    return log_entries


def _parse_log_file(s3, bucket: str, key: str) -> dict:
    """Download and parse a single log file, extracting summary stats."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        content = resp["Body"].read().decode("utf-8")
    except Exception as exc:
        return {"key": key, "error": str(exc)}

    lines = content.strip().split("\n")
    if not lines:
        return {"key": key, "lines": 0}

    header = {}
    errors = []
    warnings = []

    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if record.get("type") == "run_header":
            header = record
            continue

        level = record.get("level", "")
        message = record.get("message", "")

        if level == "ERROR":
            errors.append(message)
        elif level in ("WARNING", "WARN"):
            warnings.append(message)

    return {
        "key": key,
        "header": header,
        "total_lines": len(lines),
        "errors": errors,
        "warnings": warnings,
    }


def _analyze(runs: list[dict], log_summaries: list[dict]) -> dict:
    """Analyze runs and logs to produce health metrics."""
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "period_days": 0,
        "summary": {},
        "runs": {},
        "failures": {},
        "patterns": {},
        "recommendations": [],
    }

    # --- Run summary ---
    total_runs = len(runs)
    successful = sum(1 for r in runs if r.get("status") == "success")
    failed = sum(1 for r in runs if r.get("status") not in ("success", "running"))
    still_running = sum(1 for r in runs if r.get("status") == "running")

    runs_by_runner = Counter(r.get("runner", "unknown") for r in runs)
    runs_by_trigger = Counter(r.get("trigger", "unknown") for r in runs)

    durations = [r["duration_secs"] for r in runs if r.get("duration_secs")]
    avg_duration = sum(durations) / len(durations) if durations else 0

    # Denominator is completed runs only — exclude in-progress runs so
    # a currently-running job does not drag down the rate.
    completed = successful + failed

    report["summary"] = {
        "total_runs": total_runs,
        "successful": successful,
        "failed": failed,
        "still_running": still_running,
        "success_rate": f"{(successful / completed * 100):.1f}%" if completed else "N/A",
        "avg_duration_mins": round(avg_duration / 60, 1),
        "max_duration_mins": round(max(durations) / 60, 1) if durations else 0,
    }

    report["runs"] = {
        "by_runner": dict(runs_by_runner.most_common()),
        "by_trigger": dict(runs_by_trigger.most_common()),
    }

    # --- Error analysis ---
    all_errors = []
    all_warnings = []
    for summary in log_summaries:
        all_errors.extend(summary.get("errors", []))
        all_warnings.extend(summary.get("warnings", []))

    # Categorize errors
    error_categories = defaultdict(list)
    for err in all_errors:
        if "Splicing failed" in err or "SPLICE_FAILED" in err:
            error_categories["splice_failure"].append(err)
        elif "Transcription failed" in err or "TRANSCRIBE" in err:
            error_categories["transcribe_failure"].append(err)
        elif "Ad detection failed" in err or "DETECT" in err:
            error_categories["ad_detection_failure"].append(err)
        elif "FAILED" in err and "Step 4" in err:
            error_categories["download_failure"].append(err)
        elif "EMPTY FEED" in err:
            error_categories["empty_feed"].append(err)
        elif "manifest" in err.lower():
            error_categories["manifest_error"].append(err)
        else:
            error_categories["other"].append(err)

    report["failures"] = {
        "total_errors": len(all_errors),
        "total_warnings": len(all_warnings),
        "by_category": {k: len(v) for k, v in error_categories.items()},
        "top_errors": Counter(all_errors).most_common(10),
        "top_warnings": Counter(all_warnings).most_common(10),
    }

    # --- Pattern detection ---
    # Find repeated warnings (potential chronic issues)
    warning_counter = Counter(all_warnings)
    chronic_warnings = [(msg, count) for msg, count in warning_counter.items() if count >= 3]

    # Find stale "running" entries (possible orphaned locks)
    stale_running = []
    now = datetime.now(UTC)
    for r in runs:
        if r.get("status") == "running" and r.get("started_at"):
            try:
                started = datetime.fromisoformat(r["started_at"])
                if (now - started).total_seconds() > 7200:  # 2 hours
                    stale_running.append(r)
            except (ValueError, TypeError):
                pass

    report["patterns"] = {
        "chronic_warnings": chronic_warnings[:10],
        "stale_running_entries": len(stale_running),
    }

    # --- Recommendations ---
    recommendations = []
    if error_categories["splice_failure"]:
        recommendations.append({
            "priority": "HIGH",
            "issue": f"Splice failures: {len(error_categories['splice_failure'])} in this period",
            "action": "Check ffmpeg version/compatibility. Review affected episodes in logs.",
        })
    if error_categories["transcribe_failure"]:
        recommendations.append({
            "priority": "MEDIUM",
            "issue": f"Transcription failures: {len(error_categories['transcribe_failure'])}",
            "action": "Check AWS Transcribe quotas/limits. May need retry queue.",
        })
    if stale_running:
        recommendations.append({
            "priority": "MEDIUM",
            "issue": f"{len(stale_running)} run(s) stuck in 'running' state (>2 hours)",
            "action": "Possible orphaned lock. Check _meta/run.lock and runs.jsonl.",
        })
    if chronic_warnings:
        recommendations.append({
            "priority": "LOW",
            "issue": f"{len(chronic_warnings)} warning(s) repeated 3+ times",
            "action": "Review chronic warnings — may indicate configuration issue.",
        })
    if total_runs == 0:
        recommendations.append({
            "priority": "HIGH",
            "issue": "No runs recorded in this period",
            "action": "Check cron/scheduler is active. Verify EC2 instance is running.",
        })

    report["recommendations"] = recommendations
    return report


def _format_markdown(report: dict) -> str:
    """Format the report as human-readable Markdown."""
    lines = []
    lines.append("# PodcastDrive Health Report")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append("")

    # Summary
    s = report["summary"]
    lines.append("## Run Summary")
    lines.append(f"- **Total runs**: {s['total_runs']}")
    lines.append(f"- **Success rate**: {s['success_rate']}")
    lines.append(f"- **Failed**: {s['failed']}")
    lines.append(f"- **Avg duration**: {s['avg_duration_mins']} min")
    lines.append(f"- **Max duration**: {s['max_duration_mins']} min")
    lines.append("")

    # By runner/trigger
    lines.append("### Runs by Machine")
    for runner, count in report["runs"]["by_runner"].items():
        lines.append(f"- `{runner}`: {count}")
    lines.append("")
    lines.append("### Runs by Trigger")
    for trigger, count in report["runs"]["by_trigger"].items():
        lines.append(f"- `{trigger}`: {count}")
    lines.append("")

    # Failures
    f = report["failures"]
    lines.append("## Failures")
    lines.append(f"- **Total errors**: {f['total_errors']}")
    lines.append(f"- **Total warnings**: {f['total_warnings']}")
    lines.append("")
    if f["by_category"]:
        lines.append("### Error Categories")
        for cat, count in sorted(f["by_category"].items(), key=lambda x: -x[1]):
            lines.append(f"- `{cat}`: {count}")
        lines.append("")
    if f["top_errors"]:
        lines.append("### Top Errors")
        for msg, count in f["top_errors"][:5]:
            lines.append(f"- ({count}x) `{msg[:120]}`")
        lines.append("")

    # Recommendations
    if report["recommendations"]:
        lines.append("## Recommendations")
        for rec in report["recommendations"]:
            lines.append(f"- **[{rec['priority']}]** {rec['issue']}")
            lines.append(f"  - Action: {rec['action']}")
        lines.append("")

    # Patterns
    p = report["patterns"]
    if p.get("chronic_warnings"):
        lines.append("## Chronic Warnings (repeated 3+ times)")
        for msg, count in p["chronic_warnings"][:5]:
            lines.append(f"- ({count}x) `{msg[:120]}`")
        lines.append("")

    return "\n".join(lines)


def generate_health_report(days: int = 7, output_format: str = "md") -> str:
    """Generate a health report for the last N days.

    Args:
        days: Number of days to analyze.
        output_format: "md" for markdown, "json" for raw JSON.

    Returns:
        Formatted report string.
    """
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        return "ERROR: S3_BUCKET not set"

    s3 = boto3.client("s3")
    since = datetime.now(UTC) - timedelta(days=days)

    # Fetch data
    runs = _fetch_runs(s3, bucket, since)
    log_files = _fetch_log_files(s3, bucket, since)

    # Parse log files (limit to avoid excessive API calls)
    log_summaries = []
    for lf in log_files[:100]:  # Cap at 100 files
        summary = _parse_log_file(s3, bucket, lf["key"])
        log_summaries.append(summary)

    # Analyze
    report = _analyze(runs, log_summaries)
    report["period_days"] = days

    # Upload report to S3
    report_date = datetime.now(UTC).strftime("%Y-%m-%d")
    report_key = f"_meta/reports/{report_date}.json"
    try:
        s3.put_object(
            Bucket=bucket,
            Key=report_key,
            Body=json.dumps(report, indent=2, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("Report saved to s3://%s/%s", bucket, report_key)
    except Exception as exc:
        logger.warning("Failed to save report to S3: %s", exc)

    # Format output
    if output_format == "json":
        return json.dumps(report, indent=2, default=str)
    return _format_markdown(report)


def main():
    parser = argparse.ArgumentParser(description="PodcastDrive Health Report")
    parser.add_argument("--days", type=int, default=7, help="Days to analyze (default: 7)")
    parser.add_argument("--output", choices=["md", "json"], default="md", help="Output format")
    args = parser.parse_args()

    # Setup minimal logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    result = generate_health_report(days=args.days, output_format=args.output)
    print(result)


if __name__ == "__main__":
    main()
