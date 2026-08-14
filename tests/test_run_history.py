"""Unit tests for run_history.py — S3 append-only JSONL run history."""

import json
import os
import time
from datetime import datetime
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from run_history import HISTORY_KEY, record_run_end, record_run_start, save_run_history

BUCKET = "test-history-bucket"


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


# ---------------------------------------------------------------------------
# record_run_start
# ---------------------------------------------------------------------------


class TestRecordRunStart:
    def test_returns_dict(self):
        record = record_run_start()
        assert isinstance(record, dict)

    def test_run_id_contains_timestamp_and_pid(self):
        before = int(time.time())
        record = record_run_start()
        after = int(time.time())

        parts = record["run_id"].split("_")
        assert len(parts) == 2
        ts = int(parts[0])
        pid = int(parts[1])

        assert before <= ts <= after
        assert pid == os.getpid()

    def test_status_is_running(self):
        record = record_run_start()
        assert record["status"] == "running"

    def test_finished_at_is_none(self):
        record = record_run_start()
        assert record["finished_at"] is None

    def test_duration_is_none(self):
        record = record_run_start()
        assert record["duration_secs"] is None

    def test_started_at_is_iso_format(self):
        record = record_run_start()
        # Should parse without raising
        datetime.fromisoformat(record["started_at"])

    def test_counts_initialized_to_zero(self):
        record = record_run_start()
        assert record["podcasts_processed"] == 0
        assert record["episodes_downloaded"] == 0
        assert record["errors"] == 0

    def test_runner_from_env(self):
        with patch.dict(os.environ, {"RUNNER": "ci-machine"}):
            record = record_run_start()
        assert record["runner"] == "ci-machine"

    def test_runner_defaults_to_unknown(self):
        env = {k: v for k, v in os.environ.items() if k != "RUNNER"}
        with patch.dict(os.environ, env, clear=True):
            record = record_run_start()
        assert record["runner"] == "unknown"

    def test_trigger_from_env(self):
        with patch.dict(os.environ, {"TRIGGER": "cron"}):
            record = record_run_start()
        assert record["trigger"] == "cron"


# ---------------------------------------------------------------------------
# record_run_end
# ---------------------------------------------------------------------------


class TestRecordRunEnd:
    def test_sets_finished_at(self):
        record = record_run_start()
        result = record_run_end(record)
        assert result["finished_at"] is not None
        datetime.fromisoformat(result["finished_at"])  # valid ISO

    def test_sets_status_success_by_default(self):
        record = record_run_start()
        result = record_run_end(record)
        assert result["status"] == "success"

    def test_sets_custom_status(self):
        record = record_run_start()
        result = record_run_end(record, status="error")
        assert result["status"] == "error"

    def test_sets_podcasts_processed(self):
        record = record_run_start()
        result = record_run_end(record, podcasts_processed=5)
        assert result["podcasts_processed"] == 5

    def test_sets_episodes_downloaded(self):
        record = record_run_start()
        result = record_run_end(record, episodes_downloaded=12)
        assert result["episodes_downloaded"] == 12

    def test_sets_errors(self):
        record = record_run_start()
        result = record_run_end(record, errors=3)
        assert result["errors"] == 3

    def test_calculates_duration(self):
        record = record_run_start()
        # Sleep is patched to zero in conftest, so just check it's a non-negative int
        result = record_run_end(record)
        assert isinstance(result["duration_secs"], int)
        assert result["duration_secs"] >= 0

    def test_returns_same_record(self):
        record = record_run_start()
        result = record_run_end(record)
        assert result is record

    def test_duration_skipped_if_started_at_missing(self):
        record = record_run_start()
        record["started_at"] = "not-a-date"
        result = record_run_end(record)
        # duration_secs remains unset — original value from record_run_start is None
        # The except block means we just don't compute it; it stays as None
        assert result.get("duration_secs") is None


# ---------------------------------------------------------------------------
# save_run_history
# ---------------------------------------------------------------------------


class TestSaveRunHistory:
    def test_creates_history_file_in_s3(self, s3):
        record = record_run_start()
        record_run_end(record, status="success")
        save_run_history(record)

        resp = s3.get_object(Bucket=BUCKET, Key=HISTORY_KEY)
        content = resp["Body"].read().decode("utf-8")
        assert content.strip() != ""

    def test_saved_content_is_valid_json(self, s3):
        record = record_run_start()
        record_run_end(record)
        save_run_history(record)

        resp = s3.get_object(Bucket=BUCKET, Key=HISTORY_KEY)
        line = resp["Body"].read().decode("utf-8").strip()
        parsed = json.loads(line)
        assert parsed["run_id"] == record["run_id"]

    def test_appends_to_existing_file(self, s3):
        record1 = record_run_start()
        record_run_end(record1, status="success")
        save_run_history(record1)

        record2 = record_run_start()
        record_run_end(record2, status="error")
        save_run_history(record2)

        resp = s3.get_object(Bucket=BUCKET, Key=HISTORY_KEY)
        content = resp["Body"].read().decode("utf-8")
        lines = [l for l in content.strip().split("\n") if l.strip()]
        assert len(lines) == 2

        ids = {json.loads(l)["run_id"] for l in lines}
        assert record1["run_id"] in ids
        assert record2["run_id"] in ids

    def test_content_type_is_ndjson(self, s3):
        record = record_run_start()
        save_run_history(record)

        meta = s3.head_object(Bucket=BUCKET, Key=HISTORY_KEY)
        assert meta["ContentType"] == "application/x-ndjson"

    def test_skips_when_bucket_not_set(self, monkeypatch):
        monkeypatch.delenv("S3_BUCKET", raising=False)
        record = record_run_start()
        # Should not raise
        save_run_history(record)

    def test_handles_s3_error_gracefully(self, monkeypatch):
        """save_run_history should swallow S3 exceptions."""
        monkeypatch.setenv("S3_BUCKET", BUCKET)
        with mock_aws():
            # Bucket not created — put_object will fail
            record = record_run_start()
            save_run_history(record)  # should not raise

    def test_multiple_appends_preserve_order(self, s3):
        records = []
        for i in range(3):
            r = record_run_start()
            record_run_end(r, podcasts_processed=i)
            save_run_history(r)
            records.append(r)

        resp = s3.get_object(Bucket=BUCKET, Key=HISTORY_KEY)
        lines = [l for l in resp["Body"].read().decode("utf-8").strip().split("\n") if l]
        parsed = [json.loads(l) for l in lines]
        ids_in_order = [p["run_id"] for p in parsed]
        assert ids_in_order == [r["run_id"] for r in records]
