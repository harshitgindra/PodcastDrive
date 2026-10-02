"""Tests for the lock handling in run.sh.

The shell is where the lock is actually enforced, so the guards are tested by
running the real blocks out of ``run.sh`` — extracted verbatim — against a stub
interpreter, rather than by asserting on the text of the script.

What must hold:
  * the local flock guard names the holder and never tells anyone to delete the
    lock file (deleting it does not release the lock);
  * acquisition fails closed — exit 99 skips the run, anything else aborts it;
  * a lost lease tears the run down instead of logging and continuing;
  * secrets are tightened to 0600 at startup.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

RUN_SH = Path(__file__).resolve().parent.parent / "run.sh"
RUN_SH_TEXT = RUN_SH.read_text(encoding="utf-8")

HELPERS = ("RED='\\033[0;31m'", "section()")
SECRETS_BLOCK = ("# --- Secret file permissions ---", "done")
TRAPS_BLOCK = ("# Only a run that actually took the S3 lease", "trap on_terminate INT TERM")
FLOCK_BLOCK = ("if ! command -v flock", 'printf \'%s\\n\' "$$" >"$LOCK_FILE"')
DIST_LOCK_BLOCK = ("# --- Distributed lock (prevents concurrent runs across machines) ---", 'ok "Distributed lock held')


def _dist_lock_block() -> str:
    """The distributed-lock gate, including the `fi` that closes it."""
    block = _block(DIST_LOCK_BLOCK)
    tail = RUN_SH_TEXT[RUN_SH_TEXT.index(block) + len(block) :]
    return block + tail[: tail.index("fi\n") + 3]


def _block(markers: tuple[str, str]) -> str:
    """Return the run.sh text from *start* through the line holding *end*."""
    start, end = markers
    begin = RUN_SH_TEXT.index(start)
    stop = RUN_SH_TEXT.index(end, begin)
    stop = RUN_SH_TEXT.index("\n", stop) + 1
    return RUN_SH_TEXT[begin:stop]


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _stub_python(tmp_path: Path, exits: dict[str, int], log: Path | None = None) -> Path:
    """A stand-in for VENV_PYTHON that only understands the lock CLI."""
    log_line = f'echo "$SUBCOMMAND" >> "{log}"' if log else ":"
    return _write_script(
        tmp_path,
        "stub_python",
        textwrap.dedent(f"""\
            #!/bin/bash
            # args look like: -m distributed_lock <subcommand> [flags...]
            SUBCOMMAND=""
            for arg in "$@"; do
                case "$arg" in
                    -m|distributed_lock|--ttl|--interval) continue ;;
                    acquire|renew|release|heartbeat) SUBCOMMAND="$arg"; break ;;
                esac
            done
            {log_line}
            case "$SUBCOMMAND" in
                acquire)   echo "{exits.get("acquire_output", "acquired")}"; exit {exits.get("acquire", 0)} ;;
                heartbeat) sleep {exits.get("heartbeat_delay", 0.2)}; echo "LOST:stolen"; exit {exits.get("heartbeat", 0)} ;;
                release)   echo released; exit {exits.get("release", 0)} ;;
                *)         exit 0 ;;
            esac
            """),
    )


@pytest.fixture
def harness(tmp_path):
    """Builds a runnable script out of chosen run.sh blocks."""

    def _build(body: str, *, name="harness.sh", prelude=""):
        script = _write_script(
            tmp_path,
            name,
            "#!/bin/bash\nset -euo pipefail\n"
            + _block(HELPERS)
            + f'\nSCRIPT_DIR="{tmp_path}"\nLOG_DIR="{tmp_path}/logs"\nmkdir -p "$LOG_DIR"\n'
            + prelude
            + body,
        )
        return script

    return _build


def _run(script: Path, env_extra=None, timeout=20):
    env = {**os.environ, "PATH": os.environ["PATH"]}
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


# ---------------------------------------------------------------------------
# Local flock guard
# ---------------------------------------------------------------------------


class TestLocalFlockGuard:
    def test_first_run_acquires_and_records_its_pid(self, harness, tmp_path):
        script = harness(
            'LOCK_FILE="$SCRIPT_DIR/.podcastdrive.lock"\n' + _block(FLOCK_BLOCK) + 'echo "GOT-LOCK"\n',
        )
        result = _run(script)

        assert result.returncode == 0, result.stderr
        assert "GOT-LOCK" in result.stdout
        assert (tmp_path / ".podcastdrive.lock").read_text().strip().isdigit()

    def test_second_run_is_refused_and_names_the_holder(self, harness, tmp_path):
        guard = 'LOCK_FILE="$SCRIPT_DIR/.podcastdrive.lock"\n' + _block(FLOCK_BLOCK)
        holder = harness(guard + 'echo HOLDING; sleep 10\n', name="holder.sh")
        contender = harness(guard + 'echo "GOT-LOCK"\n', name="contender.sh")

        holder_proc = subprocess.Popen(["bash", str(holder)], stdout=subprocess.PIPE, text=True)
        try:
            assert holder_proc.stdout.readline().strip() == "HOLDING"
            result = _run(contender)
        finally:
            holder_proc.terminate()
            holder_proc.wait(timeout=10)

        assert result.returncode == 1
        assert "GOT-LOCK" not in result.stdout
        assert f"PID {holder_proc.pid}" in result.stdout
        assert "already active" in result.stdout

    def test_refusal_never_advises_deleting_the_lock_file(self, harness, tmp_path):
        """Deleting the file does not release the lock, so never suggest it."""
        guard = _block(FLOCK_BLOCK)
        assert "Remove" not in guard
        assert "do not delete the lock file" in guard

    def test_contender_does_not_truncate_the_holders_pid(self, harness, tmp_path):
        guard = 'LOCK_FILE="$SCRIPT_DIR/.podcastdrive.lock"\n' + _block(FLOCK_BLOCK)
        holder = harness(guard + 'echo HOLDING; sleep 10\n', name="holder.sh")
        contender = harness(guard + 'echo "GOT-LOCK"\n', name="contender.sh")

        holder_proc = subprocess.Popen(["bash", str(holder)], stdout=subprocess.PIPE, text=True)
        try:
            holder_proc.stdout.readline()
            _run(contender)
            recorded = (tmp_path / ".podcastdrive.lock").read_text().strip()
        finally:
            holder_proc.terminate()
            holder_proc.wait(timeout=10)

        assert recorded == str(holder_proc.pid)

    def test_missing_flock_is_reported_explicitly(self, harness, tmp_path):
        """Without flock the script must say so, not blame a phantom second run."""
        script = harness(
            'LOCK_FILE="$SCRIPT_DIR/.podcastdrive.lock"\n' + _block(FLOCK_BLOCK) + "echo GOT-LOCK\n",
        )
        # /usr/bin:/bin has no flock on macOS (it ships via Homebrew only).
        result = _run(script, env_extra={"PATH": "/usr/bin:/bin"})

        assert result.returncode == 1
        assert "flock not found" in result.stdout
        assert "GOT-LOCK" not in result.stdout


# ---------------------------------------------------------------------------
# Distributed lock gate
# ---------------------------------------------------------------------------


class TestDistributedLockGate:
    def _gate(self, harness, tmp_path, exits, extra="echo REACHED-SYNC\n"):
        stub = _stub_python(tmp_path, exits)
        return harness(
            _block(TRAPS_BLOCK) + _dist_lock_block() + extra,
            prelude=f'DRY_RUN=false\nVENV_PYTHON="{stub}"\n',
        )

    def test_successful_acquisition_proceeds_and_starts_the_heartbeat(self, harness, tmp_path):
        calls = tmp_path / "calls.log"
        stub = _stub_python(tmp_path, {"heartbeat_delay": 5}, log=calls)
        script = harness(
            # The brief sleep lets the backgrounded heartbeat reach the stub
            # before cleanup kills it; the run itself is normally far longer.
            _block(TRAPS_BLOCK) + _dist_lock_block() + "echo REACHED-SYNC\nsleep 1\n",
            prelude=f'DRY_RUN=false\nVENV_PYTHON="{stub}"\n',
        )
        result = _run(script)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "REACHED-SYNC" in result.stdout
        assert "Distributed lock held" in result.stdout
        logged = calls.read_text().split()
        assert "acquire" in logged
        assert "heartbeat" in logged
        assert "release" in logged, "cleanup must release the lease it took"

    def test_held_lock_skips_the_run_without_failing(self, harness, tmp_path):
        script = self._gate(harness, tmp_path, {"acquire": 99, "acquire_output": "LOCKED:held by mini"})
        result = _run(script)

        assert result.returncode == 0
        assert "Skipping this run" in result.stdout
        assert "REACHED-SYNC" not in result.stdout

    def test_operational_failure_fails_closed(self, harness, tmp_path):
        script = self._gate(harness, tmp_path, {"acquire": 1, "acquire_output": "ERROR:AccessDenied"})
        result = _run(script)

        assert result.returncode != 0
        assert "REACHED-SYNC" not in result.stdout, "ran without a verified lease"
        assert "refusing to run unprotected" in result.stdout

    def test_failed_acquisition_does_not_release_anyone_elses_lease(self, harness, tmp_path):
        calls = tmp_path / "calls.log"
        stub = _stub_python(tmp_path, {"acquire": 1}, log=calls)
        script = harness(
            _block(TRAPS_BLOCK) + _dist_lock_block() + "echo REACHED-SYNC\n",
            prelude=f'DRY_RUN=false\nVENV_PYTHON="{stub}"\n',
        )
        _run(script)

        assert "release" not in calls.read_text().split()

    def test_the_old_proceed_without_lock_fallback_is_gone(self):
        assert "Proceeding without distributed lock" not in RUN_SH_TEXT

    def test_dry_run_skips_the_distributed_lock_entirely(self, harness, tmp_path):
        calls = tmp_path / "calls.log"
        stub = _stub_python(tmp_path, {}, log=calls)
        script = harness(
            _block(TRAPS_BLOCK) + _dist_lock_block() + "echo REACHED-SYNC\n",
            prelude=f'DRY_RUN=true\nVENV_PYTHON="{stub}"\n',
        )
        result = _run(script)

        assert result.returncode == 0
        assert "REACHED-SYNC" in result.stdout
        assert not calls.exists()


# ---------------------------------------------------------------------------
# Lease loss must terminate the run
# ---------------------------------------------------------------------------


class TestLeaseLossTerminatesRun:
    def _long_run(self, harness, tmp_path, calls=None):
        stub = _stub_python(tmp_path, {"heartbeat": 1, "heartbeat_delay": 0.3}, log=calls)
        return harness(
            _block(TRAPS_BLOCK) + _dist_lock_block() + 'echo SYNC-STARTED\nsleep 30\necho SYNC-FINISHED\n',
            prelude=f'DRY_RUN=false\nVENV_PYTHON="{stub}"\n',
        )

    def test_lost_lease_aborts_the_run(self, harness, tmp_path):
        result = _run(self._long_run(harness, tmp_path), timeout=25)

        assert "SYNC-STARTED" in result.stdout
        assert "SYNC-FINISHED" not in result.stdout, "kept syncing without a lease"
        assert result.returncode != 0
        assert "lease lost" in result.stdout.lower()

    def test_lost_lease_does_not_release_the_replacement_holders_lease(self, harness, tmp_path):
        calls = tmp_path / "calls.log"
        _run(self._long_run(harness, tmp_path, calls=calls), timeout=25)

        assert "release" not in calls.read_text().split(), "released a lease we no longer own"

    def test_interrupt_without_lease_loss_reports_an_interrupt(self, harness, tmp_path):
        stub = _stub_python(tmp_path, {"heartbeat_delay": 30})
        script = harness(
            _block(TRAPS_BLOCK)
            + _dist_lock_block()
            + 'echo SYNC-STARTED\nkill -TERM $$\nsleep 5\necho SYNC-FINISHED\n',
            prelude=f'DRY_RUN=false\nVENV_PYTHON="{stub}"\n',
        )
        result = _run(script, timeout=25)

        assert result.returncode != 0
        assert "Interrupted" in result.stdout
        assert "SYNC-FINISHED" not in result.stdout


# ---------------------------------------------------------------------------
# Secret permissions guard
# ---------------------------------------------------------------------------


class TestSecretPermissionsGuard:
    def _run_guard(self, harness, tmp_path):
        return _run(harness(_block(SECRETS_BLOCK)))

    def test_tightens_world_readable_secrets(self, harness, tmp_path):
        config = tmp_path / "config.env"
        cookies = tmp_path / "cookies.txt"
        for path in (config, cookies):
            path.write_text("secret", encoding="utf-8")
            path.chmod(0o644)

        result = self._run_guard(harness, tmp_path)

        assert result.returncode == 0, result.stderr
        assert oct(config.stat().st_mode & 0o777) == "0o600"
        assert oct(cookies.stat().st_mode & 0o777) == "0o600"

    def test_leaves_already_private_secrets_untouched(self, harness, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("secret", encoding="utf-8")
        config.chmod(0o600)

        result = self._run_guard(harness, tmp_path)

        assert "Tightened permissions" not in result.stdout
        assert oct(config.stat().st_mode & 0o777) == "0o600"

    def test_absent_secrets_are_not_an_error(self, harness, tmp_path):
        result = self._run_guard(harness, tmp_path)
        assert result.returncode == 0
