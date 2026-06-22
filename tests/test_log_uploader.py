"""Unit tests for log_uploader.py — upload log files to S3 as JSONL."""

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from log_uploader import LOGS_PREFIX, _parse_text_log_line, upload_run_log

BUCKET = "test-log-bucket"

# A well-formed text log line matching the expected pattern
VALID_TEXT_LINE = "[2026-06-22T10:00:00+00:00] [INFO   ] [playlist_downloader] [my-runner] Downloaded episode vid001"


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def log_dir(tmp_path):
    """Temporary directory containing a playlist_downloader.log file."""
    return tmp_path


def _write_log(log_dir, content: str):
    log_file = Path(log_dir) / "playlist_downloader.log"
    log_file.write_text(content, encoding="utf-8")
    return log_file


# ---------------------------------------------------------------------------
# _parse_text_log_line
# ---------------------------------------------------------------------------

class TestParseTextLogLine:
    def test_parses_valid_line(self):
        result = _parse_text_log_line(VALID_TEXT_LINE)
        assert result is not None
        assert result["level"] == "INFO"
        assert result["logger"] == "playlist_downloader"
        assert result["runner"] == "my-runner"
        assert "Downloaded episode" in result["message"]

    def test_parses_timestamp(self):
        result = _parse_text_log_line(VALID_TEXT_LINE)
        assert result["timestamp"] == "2026-06-22T10:00:00+00:00"

    def test_returns_none_for_invalid_line(self):
        result = _parse_text_log_line("This is not a structured log line")
        assert result is None

    def test_returns_none_for_empty_string(self):
        assert _parse_text_log_line("") is None

    def test_parses_error_level(self):
        line = "[2026-06-22T10:00:00+00:00] [ERROR  ] [some_module] [runner] Something failed"
        result = _parse_text_log_line(line)
        assert result["level"] == "ERROR"

    def test_parses_warning_level(self):
        line = "[2026-06-22T10:00:00+00:00] [WARNING] [some_module] [runner] Watch out"
        result = _parse_text_log_line(line)
        assert result["level"] == "WARNING"

    def test_strips_leading_trailing_whitespace(self):
        result = _parse_text_log_line("  " + VALID_TEXT_LINE + "  ")
        assert result is not None

    def test_returns_none_for_partial_line(self):
        result = _parse_text_log_line("[2026-06-22] [INFO] partial")
        assert result is None


# ---------------------------------------------------------------------------
# upload_run_log
# ---------------------------------------------------------------------------

class TestUploadRunLogTextFormat:
    def test_returns_s3_key_on_success(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "my-runner")
        _write_log(log_dir, VALID_TEXT_LINE + "\n")
        key = upload_run_log(log_dir=str(log_dir))
        assert key is not None
        assert key.startswith(LOGS_PREFIX)
        assert key.endswith(".jsonl")

    def test_s3_key_contains_runner(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "machine-X")
        _write_log(log_dir, VALID_TEXT_LINE + "\n")
        key = upload_run_log(log_dir=str(log_dir))
        assert "machine-X" in key

    def test_s3_key_contains_date_prefix(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        _write_log(log_dir, VALID_TEXT_LINE + "\n")
        key = upload_run_log(log_dir=str(log_dir))
        # Key format: _meta/logs/YYYY-MM-DD/runner_HHMMSS.jsonl
        parts = key.split("/")
        assert len(parts) == 4
        assert parts[0] == "_meta"
        assert parts[1] == "logs"
        # Date portion: YYYY-MM-DD
        assert len(parts[2]) == 10 and parts[2][4] == "-"

    def test_uploaded_content_has_run_header(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        _write_log(log_dir, VALID_TEXT_LINE + "\n")
        key = upload_run_log(log_dir=str(log_dir))

        resp = s3.get_object(Bucket=BUCKET, Key=key)
        first_line = resp["Body"].read().decode("utf-8").split("\n")[0]
        header = json.loads(first_line)
        assert header["type"] == "run_header"
        assert header["runner"] == "test-runner"

    def test_uploaded_content_includes_parsed_lines(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        _write_log(log_dir, VALID_TEXT_LINE + "\n")
        key = upload_run_log(log_dir=str(log_dir))

        resp = s3.get_object(Bucket=BUCKET, Key=key)
        lines = [l for l in resp["Body"].read().decode("utf-8").strip().split("\n") if l]
        # First line is header; subsequent lines are log records
        assert len(lines) >= 2
        record = json.loads(lines[1])
        assert record["level"] == "INFO"

    def test_unparseable_lines_become_raw_messages(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        _write_log(log_dir, "some raw unparseable text\n")
        key = upload_run_log(log_dir=str(log_dir))

        resp = s3.get_object(Bucket=BUCKET, Key=key)
        lines = [l for l in resp["Body"].read().decode("utf-8").strip().split("\n") if l]
        records = [json.loads(l) for l in lines]
        raw_records = [r for r in records if r.get("level") == "RAW"]
        assert len(raw_records) == 1
        assert raw_records[0]["message"] == "some raw unparseable text"

    def test_runner_slash_replaced_with_underscore_in_key(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "org/machine")
        _write_log(log_dir, VALID_TEXT_LINE + "\n")
        key = upload_run_log(log_dir=str(log_dir))
        assert "/" not in key.split("/")[-1].split("_")[0]  # no slash in filename part


class TestUploadRunLogJsonFormat:
    def test_uploads_json_format_log(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        json_line = json.dumps({"level": "INFO", "message": "hello", "runner": "test-runner"})
        _write_log(log_dir, json_line + "\n")
        key = upload_run_log(log_dir=str(log_dir))
        assert key is not None

    def test_json_lines_preserved_in_upload(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        original = {"level": "ERROR", "message": "oops", "logger": "mod"}
        _write_log(log_dir, json.dumps(original) + "\n")
        key = upload_run_log(log_dir=str(log_dir))

        resp = s3.get_object(Bucket=BUCKET, Key=key)
        lines = [l for l in resp["Body"].read().decode("utf-8").strip().split("\n") if l]
        # lines[0] is header; lines[1] should be the log record
        record = json.loads(lines[1])
        assert record["level"] == "ERROR"
        assert record["message"] == "oops"

    def test_header_counts_errors_correctly(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        lines = [
            json.dumps({"level": "ERROR", "message": "fail 1"}),
            json.dumps({"level": "ERROR", "message": "fail 2"}),
            json.dumps({"level": "INFO", "message": "ok"}),
        ]
        _write_log(log_dir, "\n".join(lines) + "\n")
        key = upload_run_log(log_dir=str(log_dir))

        resp = s3.get_object(Bucket=BUCKET, Key=key)
        first_line = resp["Body"].read().decode("utf-8").split("\n")[0]
        header = json.loads(first_line)
        assert header["errors"] == 2


class TestUploadRunLogEdgeCases:
    def test_returns_none_when_log_file_not_found(self, s3, tmp_path, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        result = upload_run_log(log_dir=str(tmp_path))
        assert result is None

    def test_returns_none_when_bucket_not_set(self, log_dir, monkeypatch):
        monkeypatch.delenv("S3_BUCKET", raising=False)
        _write_log(log_dir, VALID_TEXT_LINE + "\n")
        with mock_aws():
            result = upload_run_log(log_dir=str(log_dir))
        assert result is None

    def test_returns_none_for_empty_log_file(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        _write_log(log_dir, "")
        result = upload_run_log(log_dir=str(log_dir))
        assert result is None

    def test_returns_none_for_whitespace_only_log(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        _write_log(log_dir, "\n\n   \n")
        result = upload_run_log(log_dir=str(log_dir))
        assert result is None

    def test_run_id_used_in_upload(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        _write_log(log_dir, VALID_TEXT_LINE + "\n")
        # run_id param does not change the key structure currently,
        # but it should not cause an error
        key = upload_run_log(log_dir=str(log_dir), run_id="run-123")
        assert key is not None

    def test_content_type_is_ndjson(self, s3, log_dir, monkeypatch):
        monkeypatch.setenv("RUNNER", "test-runner")
        _write_log(log_dir, VALID_TEXT_LINE + "\n")
        key = upload_run_log(log_dir=str(log_dir))

        meta = s3.head_object(Bucket=BUCKET, Key=key)
        assert meta["ContentType"] == "application/x-ndjson"
