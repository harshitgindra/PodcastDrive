"""Unit tests for distributed_lock.py — S3-backed distributed lock."""

import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from distributed_lock import DEFAULT_TTL_SECONDS, LOCK_KEY, LockAcquireError, S3Lock

BUCKET = "test-lock-bucket"


@pytest.fixture
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _make_lock(bucket=BUCKET, ttl_seconds=DEFAULT_TTL_SECONDS, runner="test-runner"):
    """Helper: create an S3Lock with a given runner env."""
    with patch.dict(os.environ, {"RUNNER": runner}):
        lock = S3Lock(bucket=bucket, ttl_seconds=ttl_seconds)
    return lock


def _put_lock(s3_client, runner="other-runner", age_seconds=0, ttl=DEFAULT_TTL_SECONDS):
    """Write a lock object directly to S3."""
    acquired_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    data = {
        "runner": runner,
        "pid": 99999,
        "acquired_at": acquired_at.isoformat(),
        "ttl_seconds": ttl,
    }
    s3_client.put_object(
        Bucket=BUCKET,
        Key=LOCK_KEY,
        Body=json.dumps(data).encode("utf-8"),
        ContentType="application/json",
    )
    return data


def _key_exists(s3_client, key):
    try:
        s3_client.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# acquire()
# ---------------------------------------------------------------------------


class TestAcquireNoExistingLock:
    def test_acquire_when_no_lock_exists(self, s3_client):
        lock = _make_lock()
        lock.acquire()
        assert lock._acquired is True
        assert _key_exists(s3_client, LOCK_KEY)

    def test_acquire_writes_correct_runner(self, s3_client):
        with patch.dict(os.environ, {"RUNNER": "machine-A"}):
            lock = S3Lock(bucket=BUCKET)
            lock.acquire()

        resp = s3_client.get_object(Bucket=BUCKET, Key=LOCK_KEY)
        data = json.loads(resp["Body"].read())
        assert data["runner"] == "machine-A"

    def test_acquire_writes_pid(self, s3_client):
        # The lock stores the PARENT PID so that a separate release subprocess
        # (which shares the same parent) can match it.
        lock = _make_lock()
        lock.acquire()

        resp = s3_client.get_object(Bucket=BUCKET, Key=LOCK_KEY)
        data = json.loads(resp["Body"].read())
        assert data["pid"] == os.getppid()

    def test_acquire_writes_ttl(self, s3_client):
        lock = _make_lock(ttl_seconds=1800)
        lock.acquire()

        resp = s3_client.get_object(Bucket=BUCKET, Key=LOCK_KEY)
        data = json.loads(resp["Body"].read())
        assert data["ttl_seconds"] == 1800


class TestAcquireWithExpiredLock:
    def test_acquire_overrides_expired_lock(self, s3_client):
        # Lock is older than its TTL
        _put_lock(s3_client, runner="old-runner", age_seconds=DEFAULT_TTL_SECONDS + 10, ttl=DEFAULT_TTL_SECONDS)

        lock = _make_lock(runner="new-runner")
        # Should NOT raise
        lock.acquire()
        assert lock._acquired is True

    def test_acquire_replaces_expired_lock_content(self, s3_client):
        _put_lock(s3_client, runner="old-runner", age_seconds=DEFAULT_TTL_SECONDS + 10)

        with patch.dict(os.environ, {"RUNNER": "new-runner"}):
            lock = S3Lock(bucket=BUCKET)
            lock.acquire()

        resp = s3_client.get_object(Bucket=BUCKET, Key=LOCK_KEY)
        data = json.loads(resp["Body"].read())
        assert data["runner"] == "new-runner"

    def test_acquire_overrides_malformed_lock(self, s3_client):
        # Malformed JSON in lock object — treated as expired
        s3_client.put_object(Bucket=BUCKET, Key=LOCK_KEY, Body=b"not-json")
        lock = _make_lock()
        lock.acquire()  # should not raise
        assert lock._acquired is True


class TestAcquireWithActiveLock:
    def test_acquire_raises_when_lock_held(self, s3_client):
        _put_lock(s3_client, runner="other-runner", age_seconds=60, ttl=DEFAULT_TTL_SECONDS)

        lock = _make_lock()
        with pytest.raises(LockAcquireError):
            lock.acquire()

    def test_lock_acquire_error_message_contains_runner(self, s3_client):
        _put_lock(s3_client, runner="blocking-runner", age_seconds=60)

        lock = _make_lock()
        with pytest.raises(LockAcquireError, match="blocking-runner"):
            lock.acquire()

    def test_acquired_stays_false_when_lock_held(self, s3_client):
        _put_lock(s3_client, runner="other-runner", age_seconds=60)

        lock = _make_lock()
        with pytest.raises(LockAcquireError):
            lock.acquire()
        assert lock._acquired is False


class TestAcquireNoBucket:
    def test_acquire_succeeds_when_bucket_not_set(self):
        with mock_aws():
            with patch.dict(os.environ, {}, clear=True):
                os.environ.pop("S3_BUCKET", None)
                lock = S3Lock(bucket="")
                lock.acquire()  # should not raise
                assert lock._acquired is True

    def test_no_s3_calls_when_bucket_not_set(self):
        with mock_aws():
            lock = S3Lock(bucket="")
            with patch.object(lock._s3, "get_object") as mock_get:
                lock.acquire()
                mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# release()
# ---------------------------------------------------------------------------


class TestRelease:
    def test_release_deletes_lock_when_owned(self, s3_client):
        with patch.dict(os.environ, {"RUNNER": "my-runner"}):
            lock = S3Lock(bucket=BUCKET)
            lock.acquire()

        # Patch lock.runner to match what was written
        lock.runner = "my-runner"
        lock.release()

        assert not _key_exists(s3_client, LOCK_KEY)
        assert lock._acquired is False

    def test_release_does_not_delete_when_lock_overridden(self, s3_client):
        with patch.dict(os.environ, {"RUNNER": "my-runner"}):
            lock = S3Lock(bucket=BUCKET)
            lock._acquired = True  # pretend we acquired it
            # But S3 has a different runner's lock
            _put_lock(s3_client, runner="other-runner", age_seconds=0)

        lock.release()
        # Lock from other-runner should still be there
        assert _key_exists(s3_client, LOCK_KEY)
        assert lock._acquired is False

    def test_release_no_bucket_is_noop(self):
        with mock_aws():
            lock = S3Lock(bucket="")
            lock._acquired = True
            lock.release()  # should not raise

    def test_release_when_not_acquired_is_safe(self, s3_client):
        lock = _make_lock()
        lock._acquired = False
        lock.release()  # should not raise

    def test_release_matches_parent_pid_from_different_subprocess(self, s3_client):
        """Simulates the run.sh pattern: acquire in subprocess A, release in subprocess B.

        Both subprocesses share the same parent (the bash script). The lock
        stores os.getppid() so that a release subprocess can verify ownership
        without needing the exact PID of the acquire subprocess.
        """
        parent_pid = os.getppid()
        data = {
            "runner": "my-runner",
            "pid": parent_pid,  # stored by acquire subprocess as os.getppid()
            "acquired_at": datetime.now(UTC).isoformat(),
            "ttl_seconds": DEFAULT_TTL_SECONDS,
        }
        s3_client.put_object(
            Bucket=BUCKET,
            Key=LOCK_KEY,
            Body=json.dumps(data).encode("utf-8"),
            ContentType="application/json",
        )

        # Release from same process (shares the same parent PID) — should succeed.
        with patch.dict(os.environ, {"RUNNER": "my-runner"}):
            lock = S3Lock(bucket=BUCKET)
            lock._acquired = True
            lock.release()

        assert not _key_exists(s3_client, LOCK_KEY)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    def test_context_manager_acquires_and_releases(self, s3_client):
        with patch.dict(os.environ, {"RUNNER": "ctx-runner"}):
            lock = S3Lock(bucket=BUCKET)
            with lock:
                # Lock should be held inside the block
                assert lock._acquired is True
                assert _key_exists(s3_client, LOCK_KEY)

        # After the block, lock should be released
        assert not _key_exists(s3_client, LOCK_KEY)

    def test_context_manager_releases_on_exception(self, s3_client):
        with patch.dict(os.environ, {"RUNNER": "ctx-runner"}):
            lock = S3Lock(bucket=BUCKET)
            try:
                with lock:
                    raise ValueError("something went wrong")
            except ValueError:
                pass

        # Lock must be released even after an exception
        assert not _key_exists(s3_client, LOCK_KEY)

    def test_context_manager_propagates_exception(self, s3_client):
        lock = _make_lock()
        with pytest.raises(RuntimeError, match="boom"):
            with lock:
                raise RuntimeError("boom")

    def test_context_manager_raises_lock_acquire_error(self, s3_client):
        _put_lock(s3_client, runner="other-runner", age_seconds=10)
        lock = _make_lock()
        with pytest.raises(LockAcquireError):
            with lock:
                pass


# ---------------------------------------------------------------------------
# _is_expired helper
# ---------------------------------------------------------------------------


class TestIsExpired:
    def test_fresh_lock_is_not_expired(self):
        with mock_aws():
            lock = S3Lock(bucket=BUCKET, ttl_seconds=3600)
            data = {
                "acquired_at": datetime.now(UTC).isoformat(),
                "ttl_seconds": 3600,
            }
            assert lock._is_expired(data) is False

    def test_old_lock_is_expired(self):
        with mock_aws():
            lock = S3Lock(bucket=BUCKET, ttl_seconds=3600)
            data = {
                "acquired_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                "ttl_seconds": 3600,
            }
            assert lock._is_expired(data) is True

    def test_malformed_lock_is_expired(self):
        with mock_aws():
            lock = S3Lock(bucket=BUCKET)
            assert lock._is_expired({}) is True
            assert lock._is_expired({"acquired_at": "not-a-date"}) is True


# ---------------------------------------------------------------------------
# _read_lock — ClientError that is NOT NoSuchKey/404 (line 63)
# ---------------------------------------------------------------------------


class TestReadLockClientError:
    def test_read_lock_reraises_non_404_client_error(self, s3_client):
        """Line 63: ClientError with code other than NoSuchKey/404 is re-raised."""
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}
        lock = _make_lock()

        with patch.object(lock._s3, "get_object", side_effect=ClientError(error_response, "GetObject")):
            with pytest.raises(ClientError) as exc_info:
                lock._read_lock()
        assert exc_info.value.response["Error"]["Code"] == "AccessDenied"

    def test_read_lock_returns_none_for_nosuchkey(self, s3_client):
        """Lines 61-62: NoSuchKey → return None (no existing key)."""
        lock = _make_lock()
        # Key doesn't exist in bucket
        result = lock._read_lock()
        assert result is None


# ---------------------------------------------------------------------------
# _write_lock_conditional — success and ClientError paths (lines 95, 99)
# ---------------------------------------------------------------------------


class TestWriteLockConditional:
    def test_returns_true_on_successful_put(self, s3_client):
        """Line 95: successful put_object with IfNoneMatch returns True."""
        lock = _make_lock()
        result = lock._write_lock_conditional()
        assert result is True
        assert _key_exists(s3_client, LOCK_KEY)

    def test_returns_false_when_lock_already_exists(self, s3_client):
        """Line 99 (PreconditionFailed path): returns False when lock exists."""
        # Write a lock first so conditional write fails
        _put_lock(s3_client, runner="other-runner", age_seconds=0)
        lock = _make_lock()
        result = lock._write_lock_conditional()
        assert result is False

    def test_reraises_non_precondition_client_error(self, s3_client):
        """Line 95 (raise path): non-412 ClientError is re-raised."""
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "NoSuchBucket", "Message": "Bucket does not exist"}}
        lock = _make_lock()

        with patch.object(lock._s3, "put_object", side_effect=ClientError(error_response, "PutObject")):
            with pytest.raises(ClientError) as exc_info:
                lock._write_lock_conditional()
        assert exc_info.value.response["Error"]["Code"] == "NoSuchBucket"


# ---------------------------------------------------------------------------
# _write_lock — unconditional write (lines 110-111)
# ---------------------------------------------------------------------------


class TestWriteLockUnconditional:
    def test_write_lock_puts_object_without_condition(self, s3_client):
        """Lines 110-111: _write_lock puts the object unconditionally."""
        lock = _make_lock()
        lock._write_lock()
        assert _key_exists(s3_client, LOCK_KEY)

        resp = s3_client.get_object(Bucket=BUCKET, Key=LOCK_KEY)
        data = json.loads(resp["Body"].read())
        assert "runner" in data
        assert "acquired_at" in data

    def test_write_lock_overwrites_existing(self, s3_client):
        """_write_lock replaces an existing lock unconditionally."""
        _put_lock(s3_client, runner="old-runner", age_seconds=0)

        with patch.dict(os.environ, {"RUNNER": "new-runner"}):
            lock = S3Lock(bucket=BUCKET)
            lock._write_lock()

        resp = s3_client.get_object(Bucket=BUCKET, Key=LOCK_KEY)
        data = json.loads(resp["Body"].read())
        assert data["runner"] == "new-runner"


# ---------------------------------------------------------------------------
# acquire() — race condition after expiry override (line 164)
# and final LockAcquireError (line 175)
# ---------------------------------------------------------------------------


class TestAcquireRaceConditionAfterExpiry:
    def test_raises_when_another_runner_wins_after_expiry_delete(self, s3_client):
        """Lines 163-164 + 167-173: after deleting expired lock, conditional write fails
        because another runner grabbed it; existing lock is read → LockAcquireError."""
        _put_lock(s3_client, runner="expired-runner", age_seconds=DEFAULT_TTL_SECONDS + 10, ttl=DEFAULT_TTL_SECONDS)

        lock = _make_lock(runner="our-runner")
        call_count = {"n": 0}

        def write_fails_on_second_call():
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call (initial try) — fail to simulate existing lock
                return False
            # Second call after delete — another runner beat us
            # Plant a fresh active lock from a competitor
            _put_lock(s3_client, runner="competitor-runner", age_seconds=0)
            return False

        with patch.object(lock, "_write_lock_conditional", side_effect=write_fails_on_second_call):
            with pytest.raises(LockAcquireError, match="competitor-runner"):
                lock.acquire()

    def test_final_raise_when_existing_is_none_after_retry(self, s3_client):
        """Line 175: LockAcquireError('Could not acquire lock...') when existing is None
        after all retries (extremely unlikely path — _read_lock returns None but
        conditional write still fails)."""
        lock = _make_lock(runner="our-runner")

        def always_return_false():
            return False

        def always_return_none():
            return None

        # Conditional write always fails, read always returns None
        with patch.object(lock, "_write_lock_conditional", side_effect=always_return_false):
            with patch.object(lock, "_read_lock", side_effect=always_return_none):
                with pytest.raises(LockAcquireError, match="Could not acquire lock"):
                    lock.acquire()
