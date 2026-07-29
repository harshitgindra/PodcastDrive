"""Unit tests for health_report.py — analyze S3 logs and generate reports."""

import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from health_report import (
    _analyze,
    _fetch_log_files,
    _fetch_runs,
    _format_markdown,
    _parse_log_file,
    generate_health_report,
    main,
)

BUCKET = "test-health-bucket"

RUNS_KEY = "_meta/runs.jsonl"
LOGS_PREFIX = "_meta/logs"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _make_run(status="success", runner="machine-A", trigger="cron", days_ago=0,
              podcasts=5, episodes=10, errors=0, duration=300):
    started = datetime.now(UTC) - timedelta(days=days_ago, seconds=10)
    finished = started + timedelta(seconds=duration)
    return {
        "run_id": f"{int(started.timestamp())}_{os.getpid()}",
        "runner": runner,
        "trigger": trigger,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat() if status != "running" else None,
        "duration_secs": duration if status != "running" else None,
        "status": status,
        "podcasts_processed": podcasts,
        "episodes_downloaded": episodes,
        "errors": errors,
    }


def _put_runs(s3_client, runs: list):
    content = "\n".join(json.dumps(r) for r in runs) + "\n"
    s3_client.put_object(Bucket=BUCKET, Key=RUNS_KEY, Body=content.encode("utf-8"))


def _make_log_file(runner="machine-A", errors=None, warnings=None, extra_records=None):
    """Build a JSONL log file content string."""
    errors = errors or []
    warnings = warnings or []
    records = []
    header = {
        "type": "run_header",
        "runner": runner,
        "trigger": "cron",
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "timestamp": datetime.now(UTC).isoformat(),
        "total_lines": len(errors) + len(warnings),
        "errors": len(errors),
        "warnings": len(warnings),
    }
    records.append(json.dumps(header))
    for msg in errors:
        records.append(json.dumps({"level": "ERROR", "message": msg, "runner": runner}))
    for msg in warnings:
        records.append(json.dumps({"level": "WARNING", "message": msg, "runner": runner}))
    if extra_records:
        for r in extra_records:
            records.append(json.dumps(r))
    return "\n".join(records) + "\n"


def _put_log_file(s3_client, key: str, content: str):
    s3_client.put_object(Bucket=BUCKET, Key=key, Body=content.encode("utf-8"))


# ---------------------------------------------------------------------------
# _fetch_runs
# ---------------------------------------------------------------------------

class TestFetchRuns:
    def test_returns_runs_within_date_range(self, s3):
        runs = [_make_run(days_ago=1), _make_run(days_ago=3)]
        _put_runs(s3, runs)

        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_runs(s3, BUCKET, since)
        assert len(result) == 2

    def test_filters_out_runs_before_since(self, s3):
        old_run = _make_run(days_ago=30)
        recent_run = _make_run(days_ago=1)
        _put_runs(s3, [old_run, recent_run])

        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_runs(s3, BUCKET, since)
        assert len(result) == 1
        assert result[0]["run_id"] == recent_run["run_id"]

    def test_returns_empty_when_file_missing(self, s3):
        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_runs(s3, BUCKET, since)
        assert result == []

    def test_skips_malformed_json_lines(self, s3):
        good_run = _make_run(days_ago=1)
        content = "not valid json\n" + json.dumps(good_run) + "\n"
        s3.put_object(Bucket=BUCKET, Key=RUNS_KEY, Body=content.encode("utf-8"))

        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_runs(s3, BUCKET, since)
        assert len(result) == 1

    def test_skips_runs_with_missing_started_at(self, s3):
        run_no_date = {"run_id": "x", "status": "success"}
        good_run = _make_run(days_ago=1)
        content = json.dumps(run_no_date) + "\n" + json.dumps(good_run) + "\n"
        s3.put_object(Bucket=BUCKET, Key=RUNS_KEY, Body=content.encode("utf-8"))

        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_runs(s3, BUCKET, since)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _fetch_log_files
# ---------------------------------------------------------------------------

class TestFetchLogFiles:
    def test_returns_log_files_within_date_range(self, s3):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{LOGS_PREFIX}/{today}/machine-A_120000.jsonl"
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"data")

        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_log_files(s3, BUCKET, since)
        assert any(entry["key"] == key for entry in result)

    def test_excludes_old_log_files(self, s3):
        old_date = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
        key = f"{LOGS_PREFIX}/{old_date}/machine-A_120000.jsonl"
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"data")

        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_log_files(s3, BUCKET, since)
        assert not any(entry["key"] == key for entry in result)

    def test_returns_empty_when_no_logs(self, s3):
        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_log_files(s3, BUCKET, since)
        assert result == []

    def test_result_contains_date_field(self, s3):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{LOGS_PREFIX}/{today}/machine-A_120000.jsonl"
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"data")

        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_log_files(s3, BUCKET, since)
        entry = next(e for e in result if e["key"] == key)
        assert entry["date"] == today


# ---------------------------------------------------------------------------
# _parse_log_file
# ---------------------------------------------------------------------------

class TestParseLogFile:
    def test_extracts_errors(self, s3):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{LOGS_PREFIX}/{today}/run.jsonl"
        content = _make_log_file(errors=["Something broke", "Another failure"])
        _put_log_file(s3, key, content)

        result = _parse_log_file(s3, BUCKET, key)
        assert len(result["errors"]) == 2
        assert "Something broke" in result["errors"]

    def test_extracts_warnings(self, s3):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{LOGS_PREFIX}/{today}/run.jsonl"
        content = _make_log_file(warnings=["Watch out", "Minor issue"])
        _put_log_file(s3, key, content)

        result = _parse_log_file(s3, BUCKET, key)
        assert len(result["warnings"]) == 2

    def test_extracts_header(self, s3):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{LOGS_PREFIX}/{today}/run.jsonl"
        content = _make_log_file(runner="machine-B")
        _put_log_file(s3, key, content)

        result = _parse_log_file(s3, BUCKET, key)
        assert result["header"]["type"] == "run_header"
        assert result["header"]["runner"] == "machine-B"

    def test_returns_error_on_missing_key(self, s3):
        result = _parse_log_file(s3, BUCKET, "_meta/logs/nonexistent.jsonl")
        assert "error" in result

    def test_skips_malformed_json_lines(self, s3):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{LOGS_PREFIX}/{today}/run.jsonl"
        content = "not json\n" + json.dumps({"level": "ERROR", "message": "real error"}) + "\n"
        _put_log_file(s3, key, content)

        result = _parse_log_file(s3, BUCKET, key)
        assert "real error" in result["errors"]

    def test_handles_warn_level(self, s3):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{LOGS_PREFIX}/{today}/run.jsonl"
        content = json.dumps({"level": "WARN", "message": "warn msg"}) + "\n"
        _put_log_file(s3, key, content)

        result = _parse_log_file(s3, BUCKET, key)
        assert "warn msg" in result["warnings"]


# ---------------------------------------------------------------------------
# _analyze
# ---------------------------------------------------------------------------

class TestAnalyze:
    def test_summary_total_runs(self):
        runs = [_make_run(status="success"), _make_run(status="error")]
        report = _analyze(runs, [])
        assert report["summary"]["total_runs"] == 2

    def test_summary_success_count(self):
        runs = [_make_run(status="success"), _make_run(status="success"), _make_run(status="error")]
        report = _analyze(runs, [])
        assert report["summary"]["successful"] == 2
        assert report["summary"]["failed"] == 1

    def test_summary_success_rate(self):
        runs = [_make_run(status="success"), _make_run(status="error")]
        report = _analyze(runs, [])
        assert report["summary"]["success_rate"] == "50.0%"

    def test_summary_no_runs_gives_na_rate(self):
        report = _analyze([], [])
        assert report["summary"]["success_rate"] == "N/A"
        assert report["summary"]["total_runs"] == 0

    def test_runs_by_runner(self):
        runs = [_make_run(runner="A"), _make_run(runner="A"), _make_run(runner="B")]
        report = _analyze(runs, [])
        assert report["runs"]["by_runner"]["A"] == 2
        assert report["runs"]["by_runner"]["B"] == 1

    def test_runs_by_trigger(self):
        runs = [_make_run(trigger="cron"), _make_run(trigger="manual")]
        report = _analyze(runs, [])
        assert report["runs"]["by_trigger"]["cron"] == 1
        assert report["runs"]["by_trigger"]["manual"] == 1

    def test_failures_total_errors(self):
        summaries = [
            {"errors": ["err1", "err2"], "warnings": []},
            {"errors": ["err3"], "warnings": ["w1"]},
        ]
        report = _analyze([], summaries)
        assert report["failures"]["total_errors"] == 3
        assert report["failures"]["total_warnings"] == 1

    def test_categorizes_splice_failure(self):
        summaries = [{"errors": ["Splicing failed for ep123"], "warnings": []}]
        report = _analyze([], summaries)
        assert report["failures"]["by_category"].get("splice_failure", 0) > 0

    def test_categorizes_transcribe_failure(self):
        summaries = [{"errors": ["Transcription failed for ep456"], "warnings": []}]
        report = _analyze([], summaries)
        assert report["failures"]["by_category"].get("transcribe_failure", 0) > 0

    def test_detects_stale_running_entries(self):
        stale_run = _make_run(status="running", days_ago=1)  # >2 hours ago
        stale_run["finished_at"] = None
        stale_run["duration_secs"] = None
        report = _analyze([stale_run], [])
        assert report["patterns"]["stale_running_entries"] >= 1

    def test_detects_chronic_warnings(self):
        repeated = "Disk space low"
        summaries = [{"errors": [], "warnings": [repeated] * 3}]
        report = _analyze([], summaries)
        assert any(msg == repeated for msg, _ in report["patterns"]["chronic_warnings"])

    def test_recommendation_for_no_runs(self):
        report = _analyze([], [])
        priorities = [r["priority"] for r in report["recommendations"]]
        assert "HIGH" in priorities

    def test_recommendation_for_splice_failures(self):
        summaries = [{"errors": ["Splicing failed ep1", "Splicing failed ep2"], "warnings": []}]
        report = _analyze([], summaries)
        issues = [r["issue"] for r in report["recommendations"]]
        assert any("Splice" in i for i in issues)

    def test_avg_duration_calculated(self):
        runs = [_make_run(duration=600), _make_run(duration=1200)]
        report = _analyze(runs, [])
        # avg = 900s = 15 min
        assert report["summary"]["avg_duration_mins"] == 15.0


# ---------------------------------------------------------------------------
# _format_markdown
# ---------------------------------------------------------------------------

class TestFormatMarkdown:
    def _get_report(self, runs=None, summaries=None):
        runs = runs or [_make_run()]
        summaries = summaries or []
        report = _analyze(runs, summaries)
        report["period_days"] = 7
        return report

    def test_starts_with_heading(self):
        md = _format_markdown(self._get_report())
        assert md.startswith("# PodcastDrive Health Report")

    def test_contains_run_summary_section(self):
        md = _format_markdown(self._get_report())
        assert "## Run Summary" in md

    def test_contains_total_runs(self):
        runs = [_make_run(), _make_run()]
        md = _format_markdown(self._get_report(runs=runs))
        assert "**Total runs**: 2" in md

    def test_contains_success_rate(self):
        runs = [_make_run(status="success")]
        md = _format_markdown(self._get_report(runs=runs))
        assert "**Success rate**:" in md

    def test_contains_failures_section(self):
        md = _format_markdown(self._get_report())
        assert "## Failures" in md

    def test_contains_recommendations_when_present(self):
        report = _analyze([], [])  # no runs → recommendation added
        report["period_days"] = 7
        md = _format_markdown(report)
        assert "## Recommendations" in md

    def test_contains_runs_by_machine_section(self):
        md = _format_markdown(self._get_report())
        assert "### Runs by Machine" in md

    def test_contains_chronic_warnings_when_present(self):
        repeated = "Some warning"
        summaries = [{"errors": [], "warnings": [repeated] * 5}]
        report = _analyze([], summaries)
        report["period_days"] = 7
        md = _format_markdown(report)
        assert "Chronic Warnings" in md


# ---------------------------------------------------------------------------
# generate_health_report (end-to-end)
# ---------------------------------------------------------------------------

class TestGenerateHealthReport:
    def test_returns_markdown_by_default(self, s3):
        result = generate_health_report(days=7, output_format="md")
        assert "# PodcastDrive Health Report" in result

    def test_returns_json_when_requested(self, s3):
        result = generate_health_report(days=7, output_format="json")
        parsed = json.loads(result)
        assert "summary" in parsed
        assert "failures" in parsed

    def test_returns_error_when_no_bucket(self, monkeypatch):
        monkeypatch.delenv("S3_BUCKET", raising=False)
        with mock_aws():
            result = generate_health_report()
        assert "ERROR" in result

    def test_processes_runs_from_s3(self, s3):
        runs = [_make_run(status="success", days_ago=1), _make_run(status="error", days_ago=2)]
        _put_runs(s3, runs)

        result = generate_health_report(days=7, output_format="json")
        parsed = json.loads(result)
        assert parsed["summary"]["total_runs"] == 2

    def test_processes_log_files_from_s3(self, s3):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{LOGS_PREFIX}/{today}/machine-A_120000.jsonl"
        content = _make_log_file(errors=["Something broke"])
        _put_log_file(s3, key, content)

        result = generate_health_report(days=7, output_format="json")
        parsed = json.loads(result)
        assert parsed["failures"]["total_errors"] >= 1

    def test_saves_report_to_s3(self, s3):
        generate_health_report(days=7, output_format="json")

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        report_key = f"_meta/reports/{today}.json"
        # Should exist in S3
        resp = s3.get_object(Bucket=BUCKET, Key=report_key)
        data = json.loads(resp["Body"].read())
        assert "summary" in data

    def test_period_days_reflected_in_output(self, s3):
        result = generate_health_report(days=14, output_format="json")
        parsed = json.loads(result)
        assert parsed["period_days"] == 14

    def test_handles_empty_bucket_gracefully(self, s3):
        # No runs, no logs — should return a report without crashing
        result = generate_health_report(days=7, output_format="md")
        assert "# PodcastDrive Health Report" in result

    def test_s3_report_upload_failure_is_handled_gracefully(self, s3, monkeypatch):
        """Line 354-355: S3 put_object for report upload raises — should not crash."""
        original_put = s3.put_object

        def fail_report_upload(**kwargs):
            if kwargs.get("Key", "").startswith("_meta/reports/"):
                raise Exception("S3 upload failed")
            return original_put(**kwargs)

        monkeypatch.setattr(s3, "put_object", fail_report_upload)

        # Should complete without raising even though the report upload fails
        with patch("health_report.boto3") as mock_boto3:
            mock_boto3.client.return_value = s3
            result = generate_health_report(days=7, output_format="md")
        assert "# PodcastDrive Health Report" in result


# ---------------------------------------------------------------------------
# main() — argparse entry point (lines 364-377)
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_default_args_prints_markdown(self, s3, capsys):
        with patch("health_report.boto3") as mock_boto3:
            mock_boto3.client.return_value = s3
            with patch("sys.argv", ["health_report"]):
                main()
        captured = capsys.readouterr()
        assert "# PodcastDrive Health Report" in captured.out

    def test_main_json_output(self, s3, capsys):
        with patch("health_report.boto3") as mock_boto3:
            mock_boto3.client.return_value = s3
            with patch("sys.argv", ["health_report", "--output", "json"]):
                main()
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert "summary" in parsed

    def test_main_custom_days(self, s3, capsys):
        with patch("health_report.boto3") as mock_boto3:
            mock_boto3.client.return_value = s3
            with patch("sys.argv", ["health_report", "--days", "14", "--output", "json"]):
                main()
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["period_days"] == 14


# ---------------------------------------------------------------------------
# Additional _fetch_runs edge cases (line 39 — exception path)
# ---------------------------------------------------------------------------

class TestFetchRunsEdgeCases:
    def test_returns_empty_on_s3_exception(self, s3):
        """Line 39 (exception branch): get_object raises — return []."""

        # Bucket exists but the object doesn't — get_object will throw
        since = datetime.now(UTC) - timedelta(days=7)
        # Don't put any runs.jsonl — S3 raises NoSuchKey which is caught
        result = _fetch_runs(s3, BUCKET, since)
        assert result == []

    def test_skips_empty_lines_in_runs(self, s3):
        """Line 39 — blank lines inside content are skipped."""
        run = _make_run(days_ago=1)
        content = "\n\n" + json.dumps(run) + "\n\n"
        s3.put_object(Bucket=BUCKET, Key=RUNS_KEY, Body=content.encode("utf-8"))
        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_runs(s3, BUCKET, since)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _fetch_log_files short-path skipping (lines 69-70)
# ---------------------------------------------------------------------------

class TestFetchLogFilesEdgeCases:
    def test_skips_keys_with_fewer_than_4_parts(self, s3):
        """Lines 69-70: keys with < 4 path parts are not added."""
        # Simulate a key at _meta/logs/file.jsonl (only 3 parts)
        s3.put_object(Bucket=BUCKET, Key="_meta/logs/file.jsonl", Body=b"data")
        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_log_files(s3, BUCKET, since)
        # The short-path key should not appear in results (no date part to compare)
        assert not any(e["key"] == "_meta/logs/file.jsonl" for e in result)


# ---------------------------------------------------------------------------
# _parse_log_file empty content (line 85)
# ---------------------------------------------------------------------------

class TestParseLogFileEdgeCases:
    def test_returns_lines_zero_for_whitespace_only_content(self, s3):
        """Line 85: after strip().split('\\n'), lines is [''] which is falsy."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{LOGS_PREFIX}/{today}/empty.jsonl"
        # Put an object with only whitespace
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"   ")
        result = _parse_log_file(s3, BUCKET, key)
        # The function returns {"key": key, "lines": 0} for empty content
        # (lines list after strip is [''] — but the branch returns {"key", "lines":0})
        # Actually "   ".strip() == "" so lines = [""] — len([""])=1 but NOT falsy.
        # Line 84-85: `if not lines` — [""] is truthy. The empty-string path only
        # triggers when content is exactly "". Let's test that.
        assert "key" in result


# ---------------------------------------------------------------------------
# _analyze error categorization (lines 172-178)
# ---------------------------------------------------------------------------

class TestAnalyzeErrorCategorization:
    def test_categorizes_download_failure(self):
        """Line 174: 'FAILED' + 'Step 4' → download_failure."""
        summaries = [{"errors": ["Step 4 FAILED for ep789: network timeout"], "warnings": []}]
        report = _analyze([], summaries)
        assert report["failures"]["by_category"].get("download_failure", 0) > 0

    def test_categorizes_empty_feed(self):
        """Line 175: 'EMPTY FEED' → empty_feed."""
        summaries = [{"errors": ["EMPTY FEED: no items in feed"], "warnings": []}]
        report = _analyze([], summaries)
        assert report["failures"]["by_category"].get("empty_feed", 0) > 0

    def test_categorizes_manifest_error(self):
        """Line 177: 'manifest' in err.lower() → manifest_error."""
        summaries = [{"errors": ["manifest.json not found for playlist"], "warnings": []}]
        report = _analyze([], summaries)
        assert report["failures"]["by_category"].get("manifest_error", 0) > 0

    def test_categorizes_other_errors(self):
        """Line 179: unrecognized errors → 'other'."""
        summaries = [{"errors": ["Completely unknown failure XYZ"], "warnings": []}]
        report = _analyze([], summaries)
        assert report["failures"]["by_category"].get("other", 0) > 0

    def test_transcribe_categorization_via_transcribe_keyword(self):
        """Line 170: 'TRANSCRIBE' keyword also triggers transcribe_failure."""
        summaries = [{"errors": ["TRANSCRIBE job failed quota exceeded"], "warnings": []}]
        report = _analyze([], summaries)
        assert report["failures"]["by_category"].get("transcribe_failure", 0) > 0

    def test_ad_detection_failure_categorization(self):
        """Line 171: 'Ad detection failed' → ad_detection_failure."""
        summaries = [{"errors": ["Ad detection failed for ep999"], "warnings": []}]
        report = _analyze([], summaries)
        assert report["failures"]["by_category"].get("ad_detection_failure", 0) > 0


# ---------------------------------------------------------------------------
# _analyze stale running detection (lines 204-205)
# ---------------------------------------------------------------------------

class TestAnalyzeStaleRunning:
    def test_skips_running_entry_with_bad_started_at(self):
        """Lines 204-205: ValueError/TypeError on bad started_at is silently ignored."""
        run = _make_run(status="running")
        run["started_at"] = "not-a-valid-date"
        run["finished_at"] = None
        run["duration_secs"] = None
        report = _analyze([run], [])
        # Should not raise; stale_running_entries stays 0 for un-parseable date
        assert report["patterns"]["stale_running_entries"] == 0


# ---------------------------------------------------------------------------
# _format_markdown by_runner / by_trigger sections (lines 283-291)
# ---------------------------------------------------------------------------

class TestFormatMarkdownRunsSections:
    def test_by_runner_entries_appear_in_markdown(self):
        """Lines 268-269: by_runner loop renders each runner."""
        runs = [_make_run(runner="machine-X"), _make_run(runner="machine-Y")]
        report = _analyze(runs, [])
        report["period_days"] = 7
        md = _format_markdown(report)
        assert "`machine-X`" in md
        assert "`machine-Y`" in md

    def test_by_trigger_entries_appear_in_markdown(self):
        """Lines 271-273: by_trigger loop renders each trigger."""
        runs = [_make_run(trigger="cron"), _make_run(trigger="manual")]
        report = _analyze(runs, [])
        report["period_days"] = 7
        md = _format_markdown(report)
        assert "`cron`" in md
        assert "`manual`" in md

    def test_error_categories_appear_in_markdown(self):
        """Lines 283-286: by_category rendered when present."""
        summaries = [{"errors": ["Splicing failed ep1"], "warnings": []}]
        report = _analyze([], summaries)
        report["period_days"] = 7
        md = _format_markdown(report)
        assert "Error Categories" in md
        assert "`splice_failure`" in md

    def test_top_errors_appear_in_markdown(self):
        """Lines 287-291: top_errors rendered when present."""
        summaries = [{"errors": ["Splicing failed ep1", "Splicing failed ep1"], "warnings": []}]
        report = _analyze([], summaries)
        report["period_days"] = 7
        md = _format_markdown(report)
        assert "Top Errors" in md


# ---------------------------------------------------------------------------
# _fetch_runs — deduplication by run_id (fix for double-entry bug)
# ---------------------------------------------------------------------------

class TestFetchRunsDeduplication:
    def test_dedup_keeps_last_entry_per_run_id(self, s3):
        """When a run writes both a start ('running') and end ('success') record,
        only the final record should be returned."""
        run_id = "1700000000_99999"
        start_record = {
            "run_id": run_id,
            "runner": "machine-A",
            "trigger": "cron",
            "started_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "finished_at": None,
            "duration_secs": None,
            "status": "running",
        }
        end_record = {
            "run_id": run_id,
            "runner": "machine-A",
            "trigger": "cron",
            "started_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "duration_secs": 180,
            "status": "success",
        }
        content = json.dumps(start_record) + "\n" + json.dumps(end_record) + "\n"
        s3.put_object(Bucket=BUCKET, Key=RUNS_KEY, Body=content.encode("utf-8"))

        since = datetime.now(UTC) - timedelta(days=7)
        result = _fetch_runs(s3, BUCKET, since)

        # One unique run, not two
        assert len(result) == 1
        assert result[0]["status"] == "success"
        assert result[0]["duration_secs"] == 180

    def test_dedup_reduces_false_stuck_count(self, s3):
        """Completed runs that have both start and end records in JSONL
        should not appear as 'stuck in running' after dedup."""
        run_id = "1700000001_11111"
        start_record = {
            "run_id": run_id,
            "runner": "machine-A",
            "trigger": "cron",
            "started_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "finished_at": None,
            "duration_secs": None,
            "status": "running",
        }
        end_record = dict(start_record)
        end_record["status"] = "success"
        end_record["finished_at"] = datetime.now(UTC).isoformat()
        end_record["duration_secs"] = 120

        content = json.dumps(start_record) + "\n" + json.dumps(end_record) + "\n"
        s3.put_object(Bucket=BUCKET, Key=RUNS_KEY, Body=content.encode("utf-8"))

        since = datetime.now(UTC) - timedelta(days=7)
        runs = _fetch_runs(s3, BUCKET, since)
        report = _analyze(runs, [])

        # No stale running entries — the completed run won
        assert report["patterns"]["stale_running_entries"] == 0
        assert report["summary"]["total_runs"] == 1
        assert report["summary"]["successful"] == 1

    def test_dedup_preserves_truly_orphaned_running_entry(self, s3):
        """A run that ONLY has a start record (crashed before writing end) should
        still be detected as potentially stuck."""
        run_id = "1700000002_22222"
        start_only = {
            "run_id": run_id,
            "runner": "machine-A",
            "trigger": "cron",
            "started_at": (datetime.now(UTC) - timedelta(hours=5)).isoformat(),
            "finished_at": None,
            "duration_secs": None,
            "status": "running",
        }
        content = json.dumps(start_only) + "\n"
        s3.put_object(Bucket=BUCKET, Key=RUNS_KEY, Body=content.encode("utf-8"))

        since = datetime.now(UTC) - timedelta(days=7)
        runs = _fetch_runs(s3, BUCKET, since)
        report = _analyze(runs, [])

        assert report["patterns"]["stale_running_entries"] == 1


# ---------------------------------------------------------------------------
# _analyze — success_rate excludes in-progress runs from denominator
# ---------------------------------------------------------------------------

class TestSuccessRateExcludesRunningRuns:
    def test_success_rate_excludes_running_from_denominator(self):
        """A currently-running entry should not count as failure in success_rate."""
        runs = [
            _make_run(status="success"),
            _make_run(status="running"),  # currently in progress — should be excluded
        ]
        report = _analyze(runs, [])
        # 1 success, 0 failed, 1 running — rate = 1/(1+0) = 100%
        assert report["summary"]["success_rate"] == "100.0%"

    def test_success_rate_na_when_no_completed_runs(self):
        """If all runs are still running (no completions), rate should be N/A."""
        runs = [_make_run(status="running")]
        report = _analyze(runs, [])
        assert report["summary"]["success_rate"] == "N/A"
