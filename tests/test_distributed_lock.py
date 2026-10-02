"""Unit tests for distributed_lock.py — S3-backed distributed run lock.

The invariants under test are the ones that make the lock safe to rely on:
ownership is a per-acquisition token (never a reused one), a stale lease is
taken over atomically, acquisition fails closed, and a lease that outlives its
TTL is kept alive by renewal rather than silently lost.
"""

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import distributed_lock
from distributed_lock import (
    DEFAULT_TTL_SECONDS,
    EXIT_LOCKED,
    LOCK_KEY,
    LockAcquireError,
    LockHeldError,
    LockRenewError,
    S3Lock,
)

BUCKET = "test-lock-bucket"


@pytest.fixture
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def owner_file(tmp_path):
    return tmp_path / "owner.token"


@pytest.fixture
def make_lock(owner_file):
    """Factory for locks whose owner token file is isolated per test."""

    def _factory(bucket=BUCKET, ttl_seconds=DEFAULT_TTL_SECONDS, runner="test-runner", **kwargs):
        kwargs.setdefault("owner_file", owner_file)
        with patch.dict(os.environ, {"RUNNER": runner}):
            return S3Lock(bucket=bucket, ttl_seconds=ttl_seconds, **kwargs)

    return _factory


def _put_lock(
    s3_client,
    runner="other-runner",
    age_seconds=0,
    ttl=DEFAULT_TTL_SECONDS,
    owner="other-owner-token",
    renewed_age=None,
    include_owner=True,
):
    """Write a lock object directly to S3, as another runner would have."""
    acquired_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    renewed_at = datetime.now(UTC) - timedelta(seconds=renewed_age if renewed_age is not None else age_seconds)
    data = {
        "runner": runner,
        "pid": 99999,
        "acquired_at": acquired_at.isoformat(),
        "renewed_at": renewed_at.isoformat(),
        "ttl_seconds": ttl,
    }
    if include_owner:
        data["owner"] = owner
    s3_client.put_object(
        Bucket=BUCKET,
        Key=LOCK_KEY,
        Body=json.dumps(data).encode("utf-8"),
        ContentType="application/json",
    )
    return data


def _read_remote_lock(s3_client):
    return json.loads(s3_client.get_object(Bucket=BUCKET, Key=LOCK_KEY)["Body"].read())


def _key_exists(s3_client, key):
    try:
        s3_client.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError:
        return False


# ---------------------------------------------------------------------------
# acquire() — happy paths
# ---------------------------------------------------------------------------


class TestAcquireNoExistingLock:
    def test_acquire_when_no_lock_exists(self, s3_client, make_lock):
        lock = make_lock()
        lock.acquire()
        assert lock._acquired is True
        assert _key_exists(s3_client, LOCK_KEY)

    def test_acquire_writes_runner_and_owner_token(self, s3_client, make_lock):
        lock = make_lock(runner="machine-A")
        lock.acquire()

        data = _read_remote_lock(s3_client)
        assert data["runner"] == "machine-A"
        assert data["owner"] == lock.owner_id
        assert len(data["owner"]) == 32

    def test_acquire_writes_ttl_and_timestamps(self, s3_client, make_lock):
        lock = make_lock(ttl_seconds=1800)
        lock.acquire()

        data = _read_remote_lock(s3_client)
        assert data["ttl_seconds"] == 1800
        assert data["acquired_at"] == data["renewed_at"]
        assert data["pid"] == os.getpid()

    def test_acquire_retries_when_holder_releases_mid_attempt(self, s3_client, make_lock):
        """Lock existed for the first PUT but is gone by the time we read it."""
        lock = make_lock()
        calls = {"n": 0}
        real_put = lock._put

        def put_once_failing(body, **conditions):
            calls["n"] += 1
            if calls["n"] == 1:
                return False  # simulate 412 from a holder that then released
            return real_put(body, **conditions)

        with patch.object(lock, "_put", side_effect=put_once_failing):
            lock.acquire()

        assert lock._acquired is True
        assert _read_remote_lock(s3_client)["owner"] == lock.owner_id


# ---------------------------------------------------------------------------
# Ownership token discipline
# ---------------------------------------------------------------------------


class TestOwnerTokenDiscipline:
    def test_acquire_never_reuses_token_from_owner_file(self, s3_client, make_lock, owner_file):
        """A stale token file from a crashed run must not become our identity."""
        owner_file.write_text("stale-token-from-previous-run", encoding="utf-8")

        lock = make_lock()
        lock.acquire()

        assert lock.owner_id != "stale-token-from-previous-run"
        assert _read_remote_lock(s3_client)["owner"] == lock.owner_id
        assert owner_file.read_text(encoding="utf-8") == lock.owner_id

    def test_stale_owner_file_does_not_release_another_runners_lock(self, s3_client, make_lock, owner_file):
        """The stale token cannot match a live lease, so release is a no-op."""
        owner_file.write_text("stale-token-from-previous-run", encoding="utf-8")
        _put_lock(s3_client, runner="live-runner", owner="live-owner-token")

        make_lock().release()

        assert _read_remote_lock(s3_client)["owner"] == "live-owner-token"

    def test_failed_acquire_leaves_existing_owner_file_untouched(self, s3_client, make_lock, owner_file):
        """A contender that loses must not overwrite the holder's token file."""
        owner_file.write_text("holder-token", encoding="utf-8")
        _put_lock(s3_client, runner="holder", age_seconds=30, owner="holder-token")

        with pytest.raises(LockAcquireError):
            make_lock(runner="contender").acquire()

        assert owner_file.read_text(encoding="utf-8") == "holder-token"

    def test_failed_acquire_does_not_create_owner_file(self, s3_client, make_lock, owner_file):
        _put_lock(s3_client, runner="holder", age_seconds=30)

        with pytest.raises(LockAcquireError):
            make_lock().acquire()

        assert not owner_file.exists()

    def test_lost_takeover_race_does_not_persist_token(self, s3_client, make_lock, owner_file):
        """Losing the If-Match replace must leave no trace of our token."""
        _put_lock(s3_client, runner="expired", age_seconds=DEFAULT_TTL_SECONDS + 10)
        lock = make_lock()

        with patch.object(lock, "_put", return_value=False):
            with pytest.raises(LockAcquireError):
                lock.acquire()

        assert not owner_file.exists()
        assert lock._acquired is False

    def test_each_acquisition_mints_a_distinct_token(self, s3_client, make_lock):
        first = make_lock()
        first.acquire()
        token_one = first.owner_id
        first.release()

        second = make_lock()
        second.acquire()

        assert second.owner_id != token_one

    def test_owner_token_file_unwritable_abandons_lease_and_aborts_run(self, s3_client, make_lock, tmp_path):
        """A cross-process runner cannot safely keep a lease without its token."""
        unwritable = tmp_path / "missing-dir-file"
        lock = make_lock(owner_file=unwritable)

        with patch("distributed_lock.os.open", side_effect=OSError("read-only fs")):
            with pytest.raises(LockAcquireError, match="token could not be stored"):
                lock.acquire()

        assert lock._acquired is False
        assert not _key_exists(s3_client, LOCK_KEY)


# ---------------------------------------------------------------------------
# acquire() — stale / malformed lease takeover
# ---------------------------------------------------------------------------


class TestAcquireTakeover:
    def test_acquire_takes_over_expired_lock(self, s3_client, make_lock):
        _put_lock(s3_client, runner="old-runner", age_seconds=DEFAULT_TTL_SECONDS + 10)

        lock = make_lock(runner="new-runner")
        lock.acquire()

        assert lock._acquired is True
        assert _read_remote_lock(s3_client)["runner"] == "new-runner"

    def test_takeover_is_a_conditional_replace_not_a_delete(self, s3_client, make_lock):
        """No delete+put window: the loser of the race cannot clobber a winner."""
        _put_lock(s3_client, runner="old-runner", age_seconds=DEFAULT_TTL_SECONDS + 10)
        lock = make_lock()

        with patch.object(lock._s3, "delete_object") as mock_delete:
            with patch.object(lock._s3, "put_object", wraps=lock._s3.put_object) as mock_put:
                lock.acquire()

        mock_delete.assert_not_called()
        conditional_puts = [c for c in mock_put.call_args_list if "IfMatch" in c.kwargs]
        assert len(conditional_puts) == 1

    def test_takeover_preserves_only_one_winner(self, s3_client, make_lock, owner_file, tmp_path):
        """Two runners racing on the same stale lease: the second must be rejected."""
        _put_lock(s3_client, runner="old-runner", age_seconds=DEFAULT_TTL_SECONDS + 10)

        winner = make_lock(runner="winner")
        loser = make_lock(runner="loser", owner_file=tmp_path / "loser.token")

        winner_etag = loser._read_lock_with_etag()[1]  # loser read the stale object first
        winner.acquire()  # winner replaces it, invalidating that etag

        assert loser._replace_lock_conditional(winner_etag, loser._new_token()) is False
        assert _read_remote_lock(s3_client)["runner"] == "winner"

    @pytest.mark.parametrize("body", [b"not-json", b"[1, 2, 3]"])
    def test_acquire_fails_closed_for_unreadable_lock(self, s3_client, make_lock, body):
        s3_client.put_object(Bucket=BUCKET, Key=LOCK_KEY, Body=body)

        with pytest.raises(LockAcquireError, match="not readable"):
            make_lock().acquire()

    def test_legacy_lock_without_owner_is_honoured_until_it_expires(self, s3_client, make_lock):
        """A lock written by the previous implementation still blocks us."""
        _put_lock(s3_client, runner="legacy-runner", age_seconds=10, include_owner=False)

        with pytest.raises(LockHeldError, match="legacy-runner"):
            make_lock().acquire()


# ---------------------------------------------------------------------------
# acquire() — contention and fail-closed behaviour
# ---------------------------------------------------------------------------


class TestAcquireWithActiveLock:
    def test_acquire_raises_lock_held_error_when_lock_held(self, s3_client, make_lock):
        _put_lock(s3_client, runner="other-runner", age_seconds=60)

        with pytest.raises(LockHeldError):
            make_lock().acquire()

    def test_lock_held_error_is_an_acquire_error(self):
        assert issubclass(LockHeldError, LockAcquireError)

    def test_error_message_names_the_holder_and_lease(self, s3_client, make_lock):
        _put_lock(s3_client, runner="blocking-runner", age_seconds=60, ttl=1800)

        with pytest.raises(LockHeldError, match="blocking-runner") as exc:
            make_lock().acquire()
        assert "1800" in str(exc.value)

    def test_acquired_stays_false_when_lock_held(self, s3_client, make_lock):
        _put_lock(s3_client, runner="other-runner", age_seconds=60)
        lock = make_lock()

        with pytest.raises(LockAcquireError):
            lock.acquire()
        assert lock._acquired is False

    def test_renewed_lease_is_not_expired_even_past_its_first_hour(self, s3_client, make_lock):
        """A long run that keeps renewing must not be stolen from."""
        _put_lock(s3_client, runner="long-runner", age_seconds=5 * DEFAULT_TTL_SECONDS, renewed_age=30)

        with pytest.raises(LockHeldError, match="long-runner"):
            make_lock().acquire()


class TestAcquireFailsClosed:
    def test_s3_error_on_write_raises_lock_acquire_error(self, s3_client, make_lock):
        lock = make_lock()
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "PutObject")

        with patch.object(lock._s3, "put_object", side_effect=error):
            with pytest.raises(LockAcquireError, match="AccessDenied"):
                lock.acquire()
        assert lock._acquired is False

    def test_s3_error_on_read_raises_lock_acquire_error(self, s3_client, make_lock):
        _put_lock(s3_client, runner="holder", age_seconds=10)
        lock = make_lock()
        error = ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "GetObject")

        with patch.object(lock._s3, "get_object", side_effect=error):
            with pytest.raises(LockAcquireError, match="InternalError"):
                lock.acquire()

    def test_unexpected_exception_is_wrapped_not_swallowed(self, s3_client, make_lock):
        lock = make_lock()

        with patch.object(lock, "_write_lock_conditional", side_effect=RuntimeError("socket exploded")):
            with pytest.raises(LockAcquireError, match="RuntimeError"):
                lock.acquire()

    def test_contended_without_readable_holder_still_raises(self, s3_client, make_lock):
        lock = make_lock()

        with patch.object(lock, "_put", return_value=False):
            with patch.object(lock, "_read_lock_with_etag", return_value=(None, None)):
                with pytest.raises(LockAcquireError, match="write refused"):
                    lock.acquire()


class TestAcquireNoBucket:
    def test_lock_is_disabled_without_a_bucket(self, make_lock):
        with mock_aws():
            lock = make_lock(bucket="")
            lock.acquire()

        assert lock.disabled is True
        assert lock._acquired is True

    def test_no_s3_calls_when_bucket_not_set(self, make_lock):
        with mock_aws():
            lock = make_lock(bucket="")
            with patch.object(lock._s3, "get_object") as mock_get:
                with patch.object(lock._s3, "put_object") as mock_put:
                    lock.acquire()
                    lock.renew()
                    lock.release()
        mock_get.assert_not_called()
        mock_put.assert_not_called()


# ---------------------------------------------------------------------------
# renew()
# ---------------------------------------------------------------------------


class TestRenew:
    def test_renew_extends_the_lease(self, s3_client, make_lock):
        lock = make_lock()
        lock.acquire()
        before = _read_remote_lock(s3_client)

        lock.renew()
        after = _read_remote_lock(s3_client)

        assert after["acquired_at"] == before["acquired_at"]
        assert after["renewed_at"] >= before["renewed_at"]
        assert after["owner"] == lock.owner_id

    def test_renew_keeps_a_lock_alive_past_its_ttl(self, s3_client, make_lock):
        lock = make_lock(ttl_seconds=60)
        lock.acquire()
        # Backdate acquisition well beyond the TTL; only renewal keeps it valid.
        stale = _read_remote_lock(s3_client)
        stale["acquired_at"] = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        s3_client.put_object(Bucket=BUCKET, Key=LOCK_KEY, Body=json.dumps(stale).encode())

        lock.renew()

        assert lock._is_expired(_read_remote_lock(s3_client)) is False

    def test_renew_from_a_separate_instance_using_the_owner_file(self, s3_client, make_lock):
        """The run.sh heartbeat is a different process; the token file is its proof."""
        holder = make_lock()
        holder.acquire()

        heartbeat = make_lock()  # same owner_file, no in-memory token
        heartbeat.renew()

        assert _read_remote_lock(s3_client)["owner"] == holder.owner_id

    def test_renew_raises_when_lease_taken_by_another_runner(self, s3_client, make_lock):
        lock = make_lock()
        lock.acquire()
        _put_lock(s3_client, runner="thief", owner="thief-token")

        with pytest.raises(LockRenewError, match="thief"):
            lock.renew()

    def test_renew_raises_when_lock_object_is_gone(self, s3_client, make_lock):
        lock = make_lock()
        lock.acquire()
        s3_client.delete_object(Bucket=BUCKET, Key=LOCK_KEY)

        with pytest.raises(LockRenewError, match="gone"):
            lock.renew()

    def test_renew_raises_without_an_ownership_token(self, s3_client, make_lock):
        with pytest.raises(LockRenewError, match="ownership token"):
            make_lock().renew()

    def test_renew_raises_when_object_changes_mid_renew(self, s3_client, make_lock):
        lock = make_lock()
        lock.acquire()

        with patch.object(lock, "_replace_lock_conditional", return_value=False):
            with pytest.raises(LockRenewError, match="changed while renewing"):
                lock.renew()

    def test_renew_raises_on_s3_read_error(self, s3_client, make_lock):
        lock = make_lock()
        lock.acquire()
        error = ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "GetObject")

        with patch.object(lock._s3, "get_object", side_effect=error):
            with pytest.raises(LockRenewError, match="Could not read lock"):
                lock.renew()

    def test_renew_raises_on_s3_write_error(self, s3_client, make_lock):
        lock = make_lock()
        lock.acquire()
        error = ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "PutObject")

        with patch.object(lock._s3, "put_object", side_effect=error):
            with pytest.raises(LockRenewError, match="Could not write renewed lock"):
                lock.renew()


class TestRenewalThread:
    def test_renewal_thread_renews_the_lease(self, s3_client, make_lock):
        lock = make_lock(renew_interval=0.01)
        lock.acquire()
        renewed = threading.Event()

        with patch.object(lock, "renew", side_effect=renewed.set):
            lock.start_renewal()
            assert renewed.wait(timeout=1), "renewal thread never called renew()"
            lock.stop_renewal()

    def test_renewal_thread_flags_a_lost_lease(self, s3_client, make_lock):
        lock = make_lock(renew_interval=0.01)
        lock.acquire()

        with patch.object(lock, "renew", side_effect=LockRenewError("lease lost")):
            lock.start_renewal()
            thread = lock._renew_thread
            thread.join(timeout=5)

        assert lock.lease_lost is True

    def test_start_renewal_is_a_noop_when_lock_not_held(self, s3_client, make_lock):
        lock = make_lock()
        lock.start_renewal()
        assert lock._renew_thread is None

    def test_start_renewal_does_not_start_two_threads(self, s3_client, make_lock):
        lock = make_lock(renew_interval=5)
        lock.acquire()
        lock.start_renewal()
        first = lock._renew_thread
        lock.start_renewal()

        assert lock._renew_thread is first
        lock.stop_renewal()

    def test_stop_renewal_without_a_thread_is_safe(self, make_lock):
        with mock_aws():
            make_lock(bucket="").stop_renewal()


# ---------------------------------------------------------------------------
# release()
# ---------------------------------------------------------------------------


class TestRelease:
    def test_release_deletes_lock_when_owned(self, s3_client, make_lock, owner_file):
        lock = make_lock()
        lock.acquire()
        lock.release()

        assert not _key_exists(s3_client, LOCK_KEY)
        assert lock._acquired is False
        assert not owner_file.exists()

    def test_release_from_a_separate_instance_using_the_owner_file(self, s3_client, make_lock):
        """run.sh releases from a cleanup subprocess, not the acquiring one."""
        make_lock().acquire()

        make_lock().release()

        assert not _key_exists(s3_client, LOCK_KEY)

    def test_release_does_not_delete_another_runners_lock(self, s3_client, make_lock, owner_file):
        owner_file.write_text("our-token", encoding="utf-8")
        lock = make_lock()
        lock._acquired = True  # we believe we hold it
        _put_lock(s3_client, runner="other-runner", owner="other-token")

        lock.release()

        assert _key_exists(s3_client, LOCK_KEY)
        assert lock._acquired is False

    def test_release_keeps_a_replacement_holders_token_file(self, s3_client, make_lock, owner_file):
        """The next holder on this machine owns the token file; do not unlink it."""
        lock = make_lock()
        lock.acquire()
        released_token = lock.owner_id
        lock.release()

        replacement = make_lock(runner="replacement")
        replacement.acquire()

        # A late release from the first run must leave the replacement's token.
        stale = make_lock(owner_id=released_token)
        stale.release()

        assert owner_file.read_text(encoding="utf-8") == replacement.owner_id
        assert _read_remote_lock(s3_client)["owner"] == replacement.owner_id

    def test_release_without_a_token_settles_local_state(self, s3_client, make_lock):
        lock = make_lock()
        lock._acquired = True
        lock.release()
        assert lock._acquired is False

    def test_release_does_not_delete_a_legacy_lock_without_owner(self, s3_client, make_lock, owner_file):
        owner_file.write_text("our-token", encoding="utf-8")
        _put_lock(s3_client, runner="legacy", include_owner=False)

        make_lock().release()

        assert _key_exists(s3_client, LOCK_KEY)

    def test_release_uses_a_conditional_delete(self, s3_client, make_lock):
        lock = make_lock()
        lock.acquire()

        with patch.object(lock._s3, "delete_object", wraps=lock._s3.delete_object) as mock_delete:
            lock.release()

        assert "IfMatch" in mock_delete.call_args.kwargs

    def test_release_leaves_lock_when_etag_changed(self, s3_client, make_lock):
        lock = make_lock()
        lock.acquire()

        with patch.object(lock, "_delete_lock", return_value=False):
            lock.release()

        assert _key_exists(s3_client, LOCK_KEY)

    def test_release_without_a_token_is_a_noop(self, s3_client, make_lock):
        lock = make_lock()
        with patch.object(lock._s3, "get_object") as mock_get:
            lock.release()
        mock_get.assert_not_called()

    def test_release_survives_s3_read_error(self, s3_client, make_lock):
        lock = make_lock()
        lock.acquire()
        error = ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "GetObject")

        with patch.object(lock._s3, "get_object", side_effect=error):
            lock.release()  # must not raise — cleanup paths depend on it

    def test_release_stops_the_renewal_thread(self, s3_client, make_lock):
        lock = make_lock(renew_interval=5)
        lock.acquire()
        lock.start_renewal()
        thread = lock._renew_thread

        lock.release()

        assert lock._renew_thread is None
        assert not thread.is_alive()

    def test_release_no_bucket_is_noop(self, make_lock):
        with mock_aws():
            lock = make_lock(bucket="")
            lock._acquired = True
            lock.release()
            assert lock._acquired is False


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    def test_context_manager_acquires_renews_and_releases(self, s3_client, make_lock):
        lock = make_lock(renew_interval=5)
        with lock:
            assert lock._acquired is True
            assert lock._renew_thread is not None
            assert _key_exists(s3_client, LOCK_KEY)

        assert not _key_exists(s3_client, LOCK_KEY)
        assert lock._renew_thread is None

    def test_context_manager_releases_on_exception(self, s3_client, make_lock):
        lock = make_lock()
        with pytest.raises(ValueError):
            with lock:
                raise ValueError("something went wrong")

        assert not _key_exists(s3_client, LOCK_KEY)

    def test_context_manager_raises_lock_held_error(self, s3_client, make_lock):
        _put_lock(s3_client, runner="other-runner", age_seconds=10)
        with pytest.raises(LockHeldError):
            with make_lock():
                pass


# ---------------------------------------------------------------------------
# _is_expired
# ---------------------------------------------------------------------------


class TestIsExpired:
    @pytest.fixture
    def lock(self, make_lock):
        with mock_aws():
            yield make_lock(ttl_seconds=3600)

    def test_fresh_lock_is_not_expired(self, lock):
        assert lock._is_expired({"renewed_at": datetime.now(UTC).isoformat(), "ttl_seconds": 3600}) is False

    def test_old_lock_is_expired(self, lock):
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        assert lock._is_expired({"renewed_at": old, "ttl_seconds": 3600}) is True

    def test_renewed_at_wins_over_acquired_at(self, lock):
        data = {
            "acquired_at": (datetime.now(UTC) - timedelta(hours=9)).isoformat(),
            "renewed_at": datetime.now(UTC).isoformat(),
            "ttl_seconds": 3600,
        }
        assert lock._is_expired(data) is False

    def test_acquired_at_used_when_never_renewed(self, lock):
        data = {"acquired_at": (datetime.now(UTC) - timedelta(hours=9)).isoformat(), "ttl_seconds": 3600}
        assert lock._is_expired(data) is True

    def test_naive_timestamp_is_treated_as_utc(self, lock):
        naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
        assert lock._is_expired({"renewed_at": naive, "ttl_seconds": 3600}) is False

    def test_malformed_lock_has_no_verifiable_expiry(self, lock):
        assert lock._lease_deadline({}) is None
        assert lock._lease_deadline({"acquired_at": "not-a-date"}) is None
        assert lock._lease_deadline({"renewed_at": datetime.now(UTC).isoformat(), "ttl_seconds": "soon"}) is None


# ---------------------------------------------------------------------------
# S3 primitives
# ---------------------------------------------------------------------------


class TestReadLock:
    def test_read_lock_reraises_non_404_client_error(self, s3_client, make_lock):
        lock = make_lock()
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "GetObject")

        with patch.object(lock._s3, "get_object", side_effect=error):
            with pytest.raises(ClientError):
                lock._read_lock()

    def test_read_lock_returns_none_when_absent(self, s3_client, make_lock):
        assert make_lock()._read_lock_with_etag() == (None, None)

    def test_read_lock_returns_etag_for_malformed_body(self, s3_client, make_lock):
        s3_client.put_object(Bucket=BUCKET, Key=LOCK_KEY, Body=b"{nope")
        data, etag = make_lock()._read_lock_with_etag()

        assert data is None
        assert etag is not None


class TestConditionalWrites:
    def test_conditional_create_succeeds_once(self, s3_client, make_lock, tmp_path):
        first = make_lock()
        second = make_lock(owner_file=tmp_path / "second.token")

        assert first._write_lock_conditional(first._new_token()) is True
        assert second._write_lock_conditional(second._new_token()) is False

    def test_conditional_create_reraises_unexpected_error(self, s3_client, make_lock):
        lock = make_lock()
        error = ClientError({"Error": {"Code": "NoSuchBucket", "Message": "gone"}}, "PutObject")

        with patch.object(lock._s3, "put_object", side_effect=error):
            with pytest.raises(ClientError):
                lock._write_lock_conditional(lock._new_token())

    def test_conditional_replace_requires_matching_etag(self, s3_client, make_lock):
        lock = make_lock()
        lock._write_lock_conditional(lock._new_token())
        _, etag = lock._read_lock_with_etag()

        assert lock._replace_lock_conditional(etag, lock._new_token()) is True
        assert lock._replace_lock_conditional(etag, lock._new_token()) is False  # etag now stale

    def test_delete_lock_reports_precondition_failure(self, s3_client, make_lock):
        lock = make_lock()
        lock._write_lock_conditional(lock._new_token())

        assert lock._delete_lock(etag='"deadbeef"') is False
        assert _key_exists(s3_client, LOCK_KEY)

    def test_delete_lock_swallows_unexpected_errors(self, s3_client, make_lock):
        lock = make_lock()
        with patch.object(lock._s3, "delete_object", side_effect=RuntimeError("network")):
            assert lock._delete_lock() is False

    def test_delete_lock_reports_unexpected_client_error(self, s3_client, make_lock):
        lock = make_lock()
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DeleteObject")
        with patch.object(lock._s3, "delete_object", side_effect=error):
            assert lock._delete_lock() is False


# ---------------------------------------------------------------------------
# CLI used by run.sh
# ---------------------------------------------------------------------------


class TestCli:
    @pytest.fixture
    def cli_env(self, s3_client, tmp_path, monkeypatch):
        monkeypatch.setenv("S3_BUCKET", BUCKET)
        monkeypatch.setenv("RUNNER", "cli-runner")
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        yield tmp_path

    def test_acquire_returns_zero(self, cli_env, capsys):
        assert distributed_lock.main(["acquire"]) == 0
        assert "acquired" in capsys.readouterr().out

    def test_acquire_returns_locked_exit_code_when_held(self, cli_env, s3_client, capsys):
        _put_lock(s3_client, runner="holder", age_seconds=10)

        assert distributed_lock.main(["acquire"]) == EXIT_LOCKED
        assert capsys.readouterr().out.startswith("LOCKED:")

    def test_acquire_returns_error_exit_code_on_s3_failure(self, cli_env, capsys):
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject")

        with patch("boto3.client") as mock_client:
            mock_client.return_value.put_object.side_effect = error
            assert distributed_lock.main(["acquire"]) == 1
            assert capsys.readouterr().out.startswith("ERROR:")

    def test_acquire_honours_ttl_flag(self, cli_env, s3_client):
        distributed_lock.main(["acquire", "--ttl", "120", "--interval", "60"])
        assert _read_remote_lock(s3_client)["ttl_seconds"] == 120

    def test_renew_then_release_round_trip(self, cli_env, s3_client, capsys):
        assert distributed_lock.main(["acquire"]) == 0
        assert distributed_lock.main(["renew"]) == 0
        assert distributed_lock.main(["release"]) == 0
        assert not _key_exists(s3_client, LOCK_KEY)

    def test_renew_reports_lost_lease(self, cli_env, s3_client, capsys):
        distributed_lock.main(["acquire"])
        capsys.readouterr()
        _put_lock(s3_client, runner="thief", owner="thief-token")

        assert distributed_lock.main(["renew"]) == 1
        assert capsys.readouterr().out.startswith("LOST:")

    def test_heartbeat_exits_nonzero_when_lease_lost(self, cli_env, s3_client, capsys):
        distributed_lock.main(["acquire"])
        capsys.readouterr()
        _put_lock(s3_client, runner="thief", owner="thief-token")

        assert distributed_lock.main(["heartbeat", "--interval", "1"]) == 1
        assert capsys.readouterr().out.startswith("LOST:")

    def test_heartbeat_never_adopts_a_replaced_owner_file(self, cli_env, s3_client, capsys):
        """An orphaned prior runner may not renew a later runner's lease."""
        assert distributed_lock.main(["acquire"]) == 0
        stale_owner = (cli_env / distributed_lock.OWNER_FILE_NAME).read_text(encoding="utf-8")
        replacement_owner = "new-run-token"
        wait_calls = 0

        def first_wait_then_stop(self, timeout=None):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls != 1:
                raise AssertionError("heartbeat kept running after losing the original lease")
            # Swap the file and remote lease only after main() has snapshotted
            # the original token, as happens when a later run takes over.
            (cli_env / distributed_lock.OWNER_FILE_NAME).write_text(replacement_owner, encoding="utf-8")
            _put_lock(s3_client, runner="replacement", owner=replacement_owner)
            return False

        with patch.object(threading.Event, "wait", first_wait_then_stop):
            assert distributed_lock.main(["heartbeat", "--interval", "1"]) == 1

        assert _read_remote_lock(s3_client)["owner"] == replacement_owner
        assert stale_owner != replacement_owner
        assert capsys.readouterr().out.endswith("LOST:Lock now owned by 'replacement' — lease lost\n")

    def test_acquire_reports_disabled_without_bucket(self, cli_env, monkeypatch, capsys):
        monkeypatch.setenv("S3_BUCKET", "")

        assert distributed_lock.main(["acquire"]) == 0
        assert "disabled" in capsys.readouterr().out
