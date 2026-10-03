"""Distributed lock using S3 for cross-machine coordination.

Acquisition is atomic: a conditional PUT (``If-None-Match: *``) fails with 412
when the lock object already exists, so only one runner can create it.  Taking
over an expired lease is equally atomic — a conditional PUT (``If-Match:
<etag>``) replaces the exact object that was read, so two runners racing on the
same stale lease cannot both win.

Ownership is a random token minted at acquisition, not the runner name or a
PID.  Two sequential runs on the same host share both of those, so a
PID/name check would let a new run delete the lease of an older run that is
still working.  The token is persisted next to the logs so the separate
subprocesses ``run.sh`` uses for renewal and release can prove ownership.

Leases are short relative to a long sync, so the holder renews:
:meth:`S3Lock.renew` extends the lease, and :meth:`S3Lock.start_renewal` runs
it on a background thread for runs that outlive the TTL.  Losing a lease mid
run is reported loudly — it means another runner may now be writing.

Acquisition is fail-closed: every unexpected S3 error is surfaced as
:class:`LockAcquireError` so callers skip the run rather than proceeding
unprotected.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

import settings

logger = logging.getLogger(__name__)

LOCK_KEY = "_meta/run.lock"
DEFAULT_TTL_SECONDS = 3600  # lease length; renewed while the run is alive
DEFAULT_RENEW_INTERVAL_SECONDS = 300
OWNER_FILE_NAME = ".run.lock.owner"

_NOT_FOUND_CODES = ("NoSuchKey", "NotFound", "404")
_PRECONDITION_CODES = ("PreconditionFailed", "412")

#: Exit code meaning "another runner holds the lock" — run.sh skips the run.
EXIT_LOCKED = 99


class LockAcquireError(Exception):
    """Raised when the lock cannot be acquired, for any reason.

    Operational failures (S3 errors, an unparseable lock object, an
    unpersistable ownership token) use this type: the caller must fail the run,
    because nothing proves whether a peer is active.
    """


class LockHeldError(LockAcquireError):
    """Raised only when a verifiably live lease belongs to another runner.

    This is the one case where skipping the run is the right answer, so it is
    the one case the CLI reports as :data:`EXIT_LOCKED`.
    """


class LockRenewError(Exception):
    """Raised when the lease could not be extended (it was lost or expired)."""


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class S3Lock:
    """Distributed lock backed by an S3 object with conditional writes.

    Usage::

        with S3Lock(bucket="my-bucket"):
            ...  # lease renewed in the background for the whole block
    """

    def __init__(
        self,
        bucket: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        lock_key: str = LOCK_KEY,
        owner_id: str | None = None,
        owner_file: str | os.PathLike[str] | None = None,
        renew_interval: int = DEFAULT_RENEW_INTERVAL_SECONDS,
    ):
        self.bucket = bucket or settings.get("S3_BUCKET")
        self.ttl_seconds = ttl_seconds
        self.lock_key = lock_key
        self.renew_interval = renew_interval
        self.runner = settings.get("RUNNER", default="unknown")
        self.owner_file = Path(owner_file) if owner_file else Path(settings.get("LOG_DIR")) / OWNER_FILE_NAME
        self._owner_id = owner_id
        self._s3 = boto3.client("s3")
        self._acquired = False
        self.lease_lost = False
        self._renew_thread: threading.Thread | None = None
        self._renew_stop = threading.Event()

    # -- ownership token ----------------------------------------------------

    @property
    def disabled(self) -> bool:
        """True when no bucket is configured, so there is nothing to coordinate."""
        return not self.bucket

    @property
    def owner_id(self) -> str | None:
        """Token proving this process owns the lease, or None if unknown.

        Falls back to the persisted token so the renew/release subprocesses
        ``run.sh`` spawns can prove ownership of a lease they did not take.
        """
        if self._owner_id is None:
            self._owner_id = self._read_owner_file()
        return self._owner_id

    def _read_owner_file(self) -> str | None:
        try:
            token = self.owner_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None

    @staticmethod
    def _new_token() -> str:
        """Mint a token in memory only.

        Acquisition must never reuse a token read from the owner file: a stale
        file from a crashed run would make a fresh contender claim ownership of
        a lease that another process may still hold.
        """
        return uuid.uuid4().hex

    def _persist_owner(self, token: str) -> None:
        """Record *token* as ours — only ever called after a successful write.

        Writing earlier would let a contender that lost the race overwrite the
        winner's token, so a later cleanup would release the wrong lease.

        Written via a temp file and rename so a reader never sees a half-written
        token, and with mode 0600 because the token authorises lock deletion.

        Raises:
            OSError: if the token cannot be stored durably.  The renew and
                release subprocesses have no other way to prove ownership, so an
                unstorable token means an unrenewable, unreleasable lease.
        """
        self.owner_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.owner_file.with_name(f"{self.owner_file.name}.{os.getpid()}.tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.owner_file)
            os.chmod(self.owner_file, 0o600)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        self._owner_id = token

    def _clear_owner_file(self, token: str) -> None:
        """Delete the token file only while it still holds *token*.

        A replacement holder on this machine will have written its own token
        there; clearing that would strip the new holder of its ability to renew
        or release.
        """
        if self._read_owner_file() != token:
            return
        try:
            self.owner_file.unlink()
        except OSError:
            pass

    # -- S3 primitives ------------------------------------------------------

    def _read_lock_with_etag(self) -> tuple[dict | None, str | None]:
        """Return ``(lock_data, etag)``.

        ``etag is None`` means the object does not exist.  ``data is None`` with
        an etag means the object exists but is not parseable as a lock.
        """
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=self.lock_key)
            etag = resp.get("ETag")
            body = resp["Body"].read().decode("utf-8")
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                return None, None
            raise
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, etag
        return (data if isinstance(data, dict) else None), etag

    def _read_lock(self) -> dict | None:
        """Read the current lock object. Returns None if absent or malformed."""
        return self._read_lock_with_etag()[0]

    def _build_lock_body(self, owner: str, acquired_at: str | None = None) -> bytes:
        now = datetime.now(timezone.utc).isoformat()
        lock_data = {
            "owner": owner,
            "runner": self.runner,
            "pid": os.getpid(),
            "acquired_at": acquired_at or now,
            "renewed_at": now,
            "ttl_seconds": self.ttl_seconds,
        }
        return json.dumps(lock_data, indent=2).encode("utf-8")

    def _put(self, body: bytes, **conditions) -> bool:
        """PUT the lock body under a precondition. False means 412 (lost the race)."""
        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=self.lock_key,
                Body=body,
                ContentType="application/json",
                **conditions,
            )
            return True
        except ClientError as exc:
            if _error_code(exc) in _PRECONDITION_CODES:
                return False
            raise

    def _write_lock_conditional(self, owner: str) -> bool:
        """Create the lock only if it does not exist yet."""
        return self._put(self._build_lock_body(owner), IfNoneMatch="*")

    def _replace_lock_conditional(self, etag: str, owner: str, acquired_at: str | None = None) -> bool:
        """Replace exactly the object identified by *etag*."""
        return self._put(self._build_lock_body(owner, acquired_at=acquired_at), IfMatch=etag)

    def _delete_lock(self, etag: str | None = None) -> bool:
        """Delete the lock, optionally only when it still matches *etag*."""
        conditions = {"IfMatch": etag} if etag else {}
        try:
            self._s3.delete_object(Bucket=self.bucket, Key=self.lock_key, **conditions)
            return True
        except ClientError as exc:
            if _error_code(exc) in _PRECONDITION_CODES + _NOT_FOUND_CODES:
                return False
            logger.warning("Failed to delete lock: %s", exc)
            return False
        except Exception as exc:
            logger.warning("Failed to delete lock: %s", exc)
            return False

    def _lease_deadline(self, lock_data: dict) -> datetime | None:
        """Instant after which the lease is dead, or None if undeterminable.

        None means "do not touch this lock": a lease may only be taken over on
        evidence that it expired, never on the absence of evidence.
        """
        try:
            stamp = lock_data.get("renewed_at") or lock_data["acquired_at"]
            last_seen = datetime.fromisoformat(stamp)
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            ttl = float(lock_data.get("ttl_seconds", DEFAULT_TTL_SECONDS))
        except (ValueError, KeyError, TypeError):
            return None
        if ttl <= 0:
            return None
        return last_seen + timedelta(seconds=ttl)

    def _is_expired(self, lock_data: dict) -> bool:
        """True only when the lease has a readable deadline that has passed."""
        deadline = self._lease_deadline(lock_data)
        return deadline is not None and datetime.now(timezone.utc) > deadline

    def _held_by_message(self, existing: dict) -> str:
        return (
            f"Lock held by '{existing.get('runner', '?')}' "
            f"since {existing.get('acquired_at', '?')} "
            f"(renewed {existing.get('renewed_at', 'never')}, "
            f"TTL {existing.get('ttl_seconds', 0)}s). "
            f"Skipping this run."
        )

    # -- public API ---------------------------------------------------------

    def acquire(self) -> None:
        """Acquire the lock, or raise :class:`LockAcquireError`.

        Fail-closed: an S3 failure, a malformed response or a lost race all
        raise, so a caller never continues believing it holds the lock.
        """
        if self.disabled:
            logger.warning("S3_BUCKET not set — distributed lock disabled, no cross-machine protection")
            self._acquired = True
            return
        try:
            self._acquire_unsafe()
        except LockAcquireError:
            raise
        except Exception as exc:
            raise LockAcquireError(f"Lock acquisition failed ({type(exc).__name__}: {exc})") from exc

    def _acquire_unsafe(self) -> None:
        # Always a brand new token: never the one left in the owner file by an
        # earlier run, and never persisted until a write has actually won.
        token = self._new_token()

        if self._write_lock_conditional(token):
            self._claim(token, "acquired")
            return

        existing, etag = self._read_lock_with_etag()

        if etag is None:
            # Holder released between our PUT and this GET — one more attempt.
            if self._write_lock_conditional(token):
                self._claim(token, "acquired after holder released")
                return
            existing, etag = self._read_lock_with_etag()

        if etag is not None and existing is None:
            # Unparseable lock object. Its owner and TTL are unknown, so a peer
            # may well be running; overwriting it would permit two concurrent
            # runs. Fail closed and let an operator decide.
            raise LockAcquireError(
                f"Lock object s3://{self.bucket}/{self.lock_key} is not readable as a lock. "
                "Refusing to take it over — inspect it and delete it manually if no run is active."
            )

        if existing is not None and self._lease_deadline(existing) is None:
            # Parseable JSON, unparseable lease: same reasoning as above.
            raise LockAcquireError(
                f"Lock held by '{existing.get('runner', '?')}' has no verifiable expiry "
                f"(acquired_at={existing.get('acquired_at', '?')!r}, ttl_seconds={existing.get('ttl_seconds', '?')!r}). "
                "Refusing to take it over."
            )

        if existing is not None and self._is_expired(existing):
            logger.warning(
                "Stale lock found (held by %s, last renewed %s) — taking over",
                existing.get("runner", "?"),
                existing.get("renewed_at", "?"),
            )
            # Conditional replace: whoever matches the etag first wins, so the
            # loser of the race never overwrites the winner's fresh lease.
            if self._replace_lock_conditional(etag, token):
                self._claim(token, "taken over (stale lease)")
                return
            existing, etag = self._read_lock_with_etag()
            if existing is not None and not self._is_expired(existing):
                raise LockHeldError(self._held_by_message(existing))
            raise LockAcquireError("Lost the race to take over a stale lock")

        if existing is not None:
            raise LockHeldError(self._held_by_message(existing))
        raise LockAcquireError("Could not acquire lock: write refused but no holder is readable")

    def _claim(self, token: str, how: str) -> None:
        """Mark the lease as ours — persisting the token only now that it is."""
        try:
            self._persist_owner(token)
        except OSError as exc:
            # Without a durable token the renew/release subprocesses cannot
            # prove ownership, so the lease would survive this run and block
            # every machine until its TTL. Give it back immediately.
            self._abandon(token)
            raise LockAcquireError(
                f"Lock {how} but the ownership token could not be stored at {self.owner_file} ({exc}); "
                "released the lease rather than leave an unrenewable one behind"
            ) from exc
        self._acquired = True
        logger.info("Distributed lock %s by %s (owner %s)", how, self.runner, token)

    def _abandon(self, token: str) -> None:
        """Delete the remote lease, but only while *token* still owns it."""
        try:
            existing, etag = self._read_lock_with_etag()
            if existing and etag and existing.get("owner") == token:
                self._delete_lock(etag)
        except ClientError as exc:
            logger.error("Could not roll back lock %s after a failed claim: %s", self.lock_key, exc)

    def renew(self) -> None:
        """Extend the lease. Raises :class:`LockRenewError` if it was lost."""
        if self.disabled:
            return
        owner = self.owner_id
        if not owner:
            raise LockRenewError("No lock ownership token — cannot renew")
        try:
            existing, etag = self._read_lock_with_etag()
        except ClientError as exc:
            raise LockRenewError(f"Could not read lock to renew: {exc}") from exc
        if etag is None or not existing:
            raise LockRenewError("Lock object is gone — lease lost")
        if existing.get("owner") != owner:
            raise LockRenewError(f"Lock now owned by '{existing.get('runner', '?')}' — lease lost")
        try:
            replaced = self._replace_lock_conditional(etag, owner, acquired_at=existing.get("acquired_at"))
        except ClientError as exc:
            raise LockRenewError(f"Could not write renewed lock: {exc}") from exc
        if not replaced:
            raise LockRenewError("Lock changed while renewing — lease lost")
        logger.debug("Lease renewed by %s for %ds", self.runner, self.ttl_seconds)

    def start_renewal(self) -> None:
        """Renew the lease on a daemon thread so runs may exceed the TTL."""
        if self.disabled or not self._acquired or self._renew_thread is not None:
            return
        self._renew_stop.clear()

        def _loop() -> None:
            while not self._renew_stop.wait(self.renew_interval):
                try:
                    self.renew()
                except LockRenewError as exc:
                    self.lease_lost = True
                    logger.error("Lease lost mid-run — another runner may now be active: %s", exc)
                    return

        self._renew_thread = threading.Thread(target=_loop, name="s3-lock-renewal", daemon=True)
        self._renew_thread.start()

    def stop_renewal(self) -> None:
        """Stop the renewal thread, if one is running."""
        self._renew_stop.set()
        thread, self._renew_thread = self._renew_thread, None
        if thread is not None:
            thread.join(timeout=5)

    def release(self) -> None:
        """Release the lock if this process owns it. Always safe to call."""
        self.stop_renewal()
        if self.disabled:
            self._acquired = False
            return
        owner = self.owner_id
        if not owner:
            logger.debug("No ownership token — nothing to release")
            self._acquired = False
            return
        try:
            existing, etag = self._read_lock_with_etag()
        except ClientError as exc:
            logger.warning("Could not read lock to release it: %s", exc)
            return
        if existing and etag and existing.get("owner") == owner:
            if self._delete_lock(etag):
                logger.info("Distributed lock released by %s", self.runner)
            else:
                logger.warning("Lock changed while releasing — left in place")
            self._acquired = False
            self._clear_owner_file(owner)
            return
        if self._acquired or existing:
            logger.warning("Lock is not ours (owner %s) — nothing to release", (existing or {}).get("runner", "none"))
        self._acquired = False
        self._clear_owner_file(owner)

    def __enter__(self):
        self.acquire()
        self.start_renewal()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False  # Don't suppress exceptions


def main(argv: list[str] | None = None) -> int:
    """CLI used by run.sh: acquire/renew/release/heartbeat in separate processes.

    Exit codes:
        0                 success
        ``EXIT_LOCKED``   another runner verifiably holds the lock — skip the run
        1                 anything else — the caller must fail the run, because
                          an operational failure proves nothing about peers
        2                 bad arguments
    """
    parser = argparse.ArgumentParser(prog="distributed_lock", description="S3 distributed run lock")
    parser.add_argument("command", choices=("acquire", "renew", "release", "heartbeat"))
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="lease length in seconds")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_RENEW_INTERVAL_SECONDS,
        help="heartbeat renewal interval in seconds (must be shorter than --ttl)",
    )
    args = parser.parse_args(argv)

    if args.ttl <= 0:
        parser.error(f"--ttl must be positive, got {args.ttl}")
    if args.interval <= 0:
        parser.error(f"--interval must be positive, got {args.interval}")
    if args.interval >= args.ttl:
        # A renewal cadence at or beyond the lease length cannot keep the lease
        # alive: it would always fire after the lock became reclaimable.
        parser.error(f"--interval ({args.interval}s) must be shorter than --ttl ({args.ttl}s)")

    lock = S3Lock(ttl_seconds=args.ttl, renew_interval=args.interval)

    if args.command == "acquire":
        try:
            lock.acquire()
        except LockHeldError as exc:
            print(f"LOCKED:{exc}")
            return EXIT_LOCKED
        except LockAcquireError as exc:
            # Operational failure: never reported as "locked", because run.sh
            # skips on EXIT_LOCKED but must fail on this.
            print(f"ERROR:{exc}")
            return 1
        print("acquired" if not lock.disabled else "disabled")
        return 0

    if args.command == "renew":
        try:
            lock.renew()
        except LockRenewError as exc:
            print(f"LOST:{exc}")
            return 1
        print("renewed")
        return 0

    if args.command == "release":
        lock.release()
        print("released")
        return 0

    # Ctrl-C belongs to run.sh: this heartbeat must wait for the runner to
    # stop it with TERM, or its nonzero exit would be mistaken for lease loss.
    # Install before reading the owner token, which can involve filesystem I/O.
    previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        # Snapshot the token: an old heartbeat must never adopt a later run's
        # token if cleanup replaces the owner file.
        lock._owner_id = lock.owner_id
        if not lock._owner_id:
            print("LOST:No lock ownership token — cannot renew")
            return 1

        # A genuine lost lease still aborts the supervising runner.
        stop = threading.Event()
        while not stop.wait(args.interval):
            try:
                lock.renew()
            except LockRenewError as exc:
                print(f"LOST:{exc}")
                return 1
        return 0
    finally:
        signal.signal(signal.SIGINT, previous_sigint)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    sys.exit(main())
