"""Failure alerting in run_mediasync.sh and its CLI handshake.

The runner is driven end to end through bash with a fake `herald`, a fake
`.venv/bin/python` and a throwaway copy of the script, so no network, no real
Herald and no real pipeline are involved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mediasync.cli import _mark_summary_sent, _notify
from mediasync.config import Config, Profile
from mediasync.pipeline import RunStats

RUNNER = Path(__file__).resolve().parents[2] / "run_mediasync.sh"

ENV_FILE_BODY = """MEDIASYNC_STORAGE=onedrive
MEDIASYNC_ONEDRIVE_REFRESH_TOKEN=super-secret-refresh-token
"""

FAKE_HERALD = """#!/usr/bin/env bash
printf '%s\\n' "$@" >>"$HERALD_CALLS"
echo "--- call end ---" >>"$HERALD_CALLS"
exit "${FAKE_HERALD_EXIT:-0}"
"""

FAKE_PYTHON = """#!/usr/bin/env bash
echo "fake mediasync ran: $*"
if [ -e "${MEDIASYNC_NOTIFY_SENTINEL:-/nonexistent}" ]; then
    echo "sentinel: present"
else
    echo "sentinel: absent"
fi
if [ -n "${FAKE_TOUCH_SENTINEL:-}" ]; then
    : >"$MEDIASYNC_NOTIFY_SENTINEL"
fi
exit "${FAKE_PYTHON_EXIT:-0}"
"""


@pytest.fixture
def project(tmp_path):
    """A throwaway copy of the runner with fake python/herald next to it."""
    root = tmp_path / "proj"
    root.mkdir()
    shutil.copy(RUNNER, root / "run_mediasync.sh")
    (root / "mediasync.env").write_text(ENV_FILE_BODY)

    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text(FAKE_PYTHON)
    python.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    herald = fake_bin / "herald"
    herald.write_text(FAKE_HERALD)
    herald.chmod(0o755)

    return SimpleNamespace(
        root=root,
        script=root / "run_mediasync.sh",
        env_file=root / "mediasync.env",
        python=python,
        fake_bin=fake_bin,
        herald_calls=tmp_path / "herald_calls.txt",
    )


def run_runner(project, *, args=(), env=None, with_herald=True):
    base_path = f"{project.fake_bin}:{os.environ['PATH']}" if with_herald else "/usr/bin:/bin"
    full_env = {
        "PATH": base_path,
        "HOME": str(project.root),
        "HERALD_CALLS": str(project.herald_calls),
    }
    full_env.update(env or {})
    proc = subprocess.run(
        ["bash", str(project.script), *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
    )
    calls = project.herald_calls.read_text() if project.herald_calls.exists() else ""
    logs = sorted((project.root / "logs").glob("mediasync-*.log"))
    log_text = logs[-1].read_text() if logs else ""
    return proc, calls, log_text


class TestRunnerAlertsOnEarlyFailure:
    def test_crash_without_summary_alerts_once(self, project):
        """A config error or unhandled exception exits before cli._notify, so the
        runner is the only thing that can report it."""
        proc, calls, log_text = run_runner(project, env={"FAKE_PYTHON_EXIT": "1"})

        assert proc.returncode == 1
        assert calls.count("--- call end ---") == 1
        assert "MediaSync FAILED (exit 1)" in calls
        assert "Exited before sending a run summary." in calls
        assert "mediasync-" in calls  # the log path is included
        # stderr and the log are untouched by the alerting.
        assert "MediaSync failed (exit 1)" in proc.stderr
        assert "fake mediasync ran" in log_text

    def test_missing_env_file_alerts(self, project):
        project.env_file.unlink()
        proc, calls, _ = run_runner(project)

        assert proc.returncode == 1
        assert "mediasync.env is missing" in calls
        assert "not found" in proc.stderr

    def test_unusable_venv_alerts(self, project):
        project.python.unlink()
        proc, calls, _ = run_runner(project)

        assert proc.returncode == 1
        assert "No usable .venv" in calls
        assert "python3 -m venv .venv" in proc.stderr

    def test_exact_exit_code_is_preserved(self, project):
        proc, calls, _ = run_runner(project, env={"FAKE_PYTHON_EXIT": "42"})

        assert proc.returncode == 42
        assert "MediaSync FAILED (exit 42)" in calls

    def test_job_id_routes_the_alert(self, project):
        _, calls, _ = run_runner(
            project, env={"FAKE_PYTHON_EXIT": "1", "HERALD_JOB_ID": "mediasync"}
        )

        assert "--job\nmediasync" in calls


class TestRunnerDoesNotDuplicate:
    def test_delivered_summary_suppresses_the_alert(self, project):
        """`stats.failed > 0` exits 1 *after* cli._notify already sent a summary.
        Alerting again would be a second message about the same run."""
        proc, calls, log_text = run_runner(
            project, env={"FAKE_PYTHON_EXIT": "1", "FAKE_TOUCH_SENTINEL": "1"}
        )

        assert proc.returncode == 1
        assert calls == ""
        assert "run summary already delivered" in log_text

    def test_success_alerts_nothing(self, project):
        proc, calls, _ = run_runner(project, env={"FAKE_PYTHON_EXIT": "0"})

        assert proc.returncode == 0
        assert calls == ""

    def test_sentinel_is_exported_but_not_pre_created(self, project):
        """The CLI must be the only thing that creates it, or every failure would
        look like an already-reported one."""
        proc, _, log_text = run_runner(project, env={"FAKE_PYTHON_EXIT": "0"})

        assert proc.returncode == 0
        assert "sentinel: absent" in log_text


class TestRunnerDegradesSafely:
    def test_herald_disabled_sends_nothing(self, project):
        project.env_file.write_text(ENV_FILE_BODY + "MEDIASYNC_HERALD_ENABLED=false\n")
        proc, calls, _ = run_runner(project, env={"FAKE_PYTHON_EXIT": "1"})

        assert proc.returncode == 1
        assert calls == ""

    def test_missing_herald_binary_keeps_exit_code(self, project):
        proc, calls, _ = run_runner(
            project, env={"FAKE_PYTHON_EXIT": "7"}, with_herald=False
        )

        assert proc.returncode == 7
        assert calls == ""
        assert "herald not on PATH" in proc.stderr

    def test_undelivered_alert_is_reported(self, project):
        proc, _, _ = run_runner(
            project, env={"FAKE_PYTHON_EXIT": "1", "FAKE_HERALD_EXIT": "3"}
        )

        assert proc.returncode == 1
        assert "failure alert not delivered" in proc.stderr

    def test_no_secrets_in_the_alert(self, project):
        """The alert names the log file; it never forwards env or log contents,
        which hold OAuth tokens."""
        _, calls, _ = run_runner(project, env={"FAKE_PYTHON_EXIT": "1"})

        assert "super-secret-refresh-token" not in calls
        assert "fake mediasync ran" not in calls


def _config(**kw):
    base = dict(
        notion_token="t", notion_database_id="d",
        s3_bucket="b", s3_region="r", s3_prefix="p",
        profiles=[Profile("x")],
        herald_enabled=True, herald_job_id="j",
    )
    base.update(kw)
    return Config(**base)


class TestMarkSummarySent:
    def test_touches_the_sentinel(self, tmp_path, monkeypatch):
        sentinel = tmp_path / "summary-sent"
        monkeypatch.setenv("MEDIASYNC_NOTIFY_SENTINEL", str(sentinel))

        _mark_summary_sent()

        assert sentinel.exists()

    def test_no_sentinel_configured_is_a_noop(self, monkeypatch):
        monkeypatch.delenv("MEDIASYNC_NOTIFY_SENTINEL", raising=False)
        _mark_summary_sent()

    def test_unwritable_path_warns_rather_than_raising(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("MEDIASYNC_NOTIFY_SENTINEL", str(tmp_path / "nope" / "s"))

        with caplog.at_level("WARNING", logger="mediasync.cli"):
            _mark_summary_sent()

        assert "Could not write notify sentinel" in caplog.text

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/herald")
    def test_delivered_summary_marks_sentinel(self, mock_which, mock_run, tmp_path, monkeypatch):
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        sentinel = tmp_path / "summary-sent"
        monkeypatch.setenv("MEDIASYNC_NOTIFY_SENTINEL", str(sentinel))

        _notify(_config(), RunStats(processed=1, failed=2), 10)

        assert sentinel.exists()

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/herald")
    def test_undelivered_summary_leaves_it_to_the_runner(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        """--strict exit means nothing arrived, so the runner should still alert."""
        mock_run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="no recipients")
        sentinel = tmp_path / "summary-sent"
        monkeypatch.setenv("MEDIASYNC_NOTIFY_SENTINEL", str(sentinel))

        _notify(_config(), RunStats(failed=1), 10)

        assert not sentinel.exists()
