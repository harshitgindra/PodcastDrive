"""Tests for src/notifier.py — Herald-based run notifications."""

import subprocess
from unittest.mock import patch

from notifier import (
    _format_message,
    _herald_available,
    _herald_version_supported,
    _send_via_herald,
    send_run_notification,
)


class TestFormatMessage:
    """Tests for _format_message() — message content formatting."""

    def test_success_with_new_episodes(self, monkeypatch):
        monkeypatch.setenv("RUNNER", "mac-mini")
        results = [
            {"name": "MKBHD", "new_episodes": 2, "failed": 0, "bot_detected": False},
            {"name": "Huberman", "new_episodes": 0, "failed": 0, "bot_detected": False},
        ]
        msg = _format_message(results, elapsed_secs=134, status="success")
        assert "mac-mini" in msg
        assert "2m 14s" in msg
        assert "✅ MKBHD — 2 new" in msg
        assert "— Huberman — up to date" in msg
        assert "✅ 2 downloaded" in msg
        assert "📡 PodcastDrive — mac-mini" in msg
        assert "*" not in msg  # plain mode: Telegram must not see markdown

    def test_names_with_markdown_characters_are_not_escaped_or_broken(self, monkeypatch):
        """Plain mode means user data passes through verbatim."""
        monkeypatch.setenv("RUNNER", "test")
        results = [{"name": "All-In *with* Chamath_", "new_episodes": 1}]
        msg = _format_message(results, elapsed_secs=1, status="success")
        assert "All-In *with* Chamath_" in msg

    def test_failure_message(self, monkeypatch):
        monkeypatch.setenv("RUNNER", "ec2")
        results = [
            {"name": "ThinkSchool", "new_episodes": 0, "failed": 2, "bot_detected": False},
        ]
        msg = _format_message(results, elapsed_secs=55, status="partial_failure")
        assert "ThinkSchool" in msg
        assert "2 failed" in msg
        assert "⚠️" in msg

    def test_bot_detected(self, monkeypatch):
        monkeypatch.setenv("RUNNER", "test")
        results = [{"name": "Lex", "new_episodes": 0, "failed": 0, "bot_detected": True}]
        msg = _format_message(results, elapsed_secs=10, status="success")
        assert "bot detected" in msg

    def test_error_message(self, monkeypatch):
        monkeypatch.setenv("RUNNER", "test")
        results = [{"name": "Broken", "error": "Connection timeout"}]
        msg = _format_message(results, elapsed_secs=5, status="failure")
        assert "Broken" in msg
        assert "Connection timeout" in msg

    def test_empty_results(self, monkeypatch):
        monkeypatch.setenv("RUNNER", "test")
        msg = _format_message([], elapsed_secs=3, status="success")
        assert "0 downloaded" in msg

    def test_splice_failed_warns_and_counts(self, monkeypatch):
        """A splice failure means ads were not removed — warn even on a success run."""
        monkeypatch.setenv("RUNNER", "test")
        results = [{"name": "Acquired", "new_episodes": 2, "failed": 0, "splice_failed": 1}]
        msg = _format_message(results, elapsed_secs=30, status="success")
        assert "⚠️ Acquired — 1 splice failed (ads not removed, will retry)" in msg
        assert "1 splice failed" in msg.splitlines()[-1]

    def test_splice_failure_downgrades_a_successful_run(self, monkeypatch):
        """status=success plus a splice failure must not show a green footer."""
        monkeypatch.setenv("RUNNER", "test")
        results = [{"name": "Acquired", "new_episodes": 1, "splice_failed": 2}]
        msg = _format_message(results, elapsed_secs=30, status="success")
        footer = msg.splitlines()[-1]
        assert footer.startswith("⚠️")
        assert "1 downloaded" in footer and "2 splice failed" in footer

    def test_error_outranks_splice_failure(self, monkeypatch):
        """One line per podcast: the hard error is the one worth showing."""
        monkeypatch.setenv("RUNNER", "test")
        results = [{"name": "Acquired", "splice_failed": 1, "error": "download timeout"}]
        msg = _format_message(results, elapsed_secs=5, status="failure")
        assert "❌ Acquired — download timeout" in msg
        assert "splice failed (ads not removed" not in msg

    def test_mixed_results(self, monkeypatch):
        monkeypatch.setenv("RUNNER", "test")
        results = [
            {"name": "A", "new_episodes": 3, "failed": 0},
            {"name": "B", "new_episodes": 1, "failed": 1},
            {"name": "C", "new_episodes": 0, "failed": 0, "bot_detected": True},
            {"name": "D", "error": "timeout"},
        ]
        msg = _format_message(results, elapsed_secs=200, status="partial_failure")
        assert "✅ A — 3 new" in msg
        assert "❌ B — 1 failed, 1 new" in msg
        assert "⚠️ C — bot detected" in msg
        assert "❌ D — timeout" in msg
        assert "⚠️ 4 downloaded, 1 failed" in msg


class TestHeraldAvailable:
    """Tests for _herald_available() — checks PATH."""

    @patch("notifier.shutil.which", return_value="/usr/local/bin/herald")
    def test_available(self, mock_which):
        assert _herald_available() is True
        mock_which.assert_called_once_with("herald")

    @patch("notifier.shutil.which", return_value=None)
    def test_not_available(self, mock_which):
        assert _herald_available() is False


class TestHeraldVersionSupported:
    """Tests for _herald_version_supported() — version guard."""

    @patch("notifier.subprocess.run")
    def test_version_0_5_2_supported(self, mock_run):
        """Boundary: exactly 0.5.2 is supported (inclusive) — --strict landed here."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="0.5.2\n", stderr=""
        )
        assert _herald_version_supported() is True

    @patch("notifier.subprocess.run")
    def test_version_0_6_0_supported(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="0.6.0\n", stderr=""
        )
        assert _herald_version_supported() is True

    @patch("notifier.subprocess.run")
    def test_version_0_5_1_not_supported(self, mock_run):
        """Just below the boundary: --strict does not exist yet, so refuse."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="0.5.1\n", stderr=""
        )
        assert _herald_version_supported() is False

    @patch("notifier.subprocess.run")
    def test_version_0_2_0_not_supported(self, mock_run):
        """The old minimum is no longer enough."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="0.2.0\n", stderr=""
        )
        assert _herald_version_supported() is False

    @patch("notifier.subprocess.run")
    def test_version_1_0_0_supported(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="1.0.0\n", stderr=""
        )
        assert _herald_version_supported() is True

    @patch("notifier.subprocess.run")
    def test_version_0_1_0_not_supported(self, mock_run):
        """Old version: returns False, no exception."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="0.1.0\n", stderr=""
        )
        assert _herald_version_supported() is False

    @patch("notifier.subprocess.run")
    def test_garbage_output(self, mock_run):
        """Non-version output: returns False, no exception."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="not a version\n", stderr=""
        )
        assert _herald_version_supported() is False

    @patch("notifier.subprocess.run")
    def test_empty_output(self, mock_run):
        """Empty output: returns False, no exception."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="", stderr=""
        )
        assert _herald_version_supported() is False

    @patch("notifier.subprocess.run")
    def test_nonzero_exit(self, mock_run):
        """Herald version exits non-zero: returns False."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=1, stdout="", stderr="error"
        )
        assert _herald_version_supported() is False

    @patch("notifier.subprocess.run", side_effect=FileNotFoundError)
    def test_file_not_found(self, mock_run):
        """Herald not on PATH during version check: returns False, no raise."""
        assert _herald_version_supported() is False

    @patch("notifier.subprocess.run", side_effect=subprocess.TimeoutExpired("herald", 10))
    def test_timeout(self, mock_run):
        """Version check times out: returns False, no raise."""
        assert _herald_version_supported() is False

    @patch("notifier.subprocess.run")
    def test_non_numeric_version_parts(self, mock_run):
        """Three dot-separated but non-numeric parts: caught, not raised."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="a.b.c\n", stderr=""
        )
        assert _herald_version_supported() is False

    @patch("notifier.subprocess.run", side_effect=OSError("exec format error"))
    def test_os_error_during_version_check(self, mock_run):
        assert _herald_version_supported() is False

    @patch("notifier.subprocess.run")
    def test_version_check_calls_correct_command(self, mock_run):
        """Verifies the exact subprocess call."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="0.5.2\n", stderr=""
        )
        _herald_version_supported()
        mock_run.assert_called_once_with(
            ["herald", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )


class TestSendViaHerald:
    """Tests for _send_via_herald() — subprocess call and reply routing."""

    @patch("notifier.subprocess.run")
    def test_successful_send_without_job(self, mock_run, monkeypatch):
        """Cron/manual run: no HERALD_JOB_ID, so no --job flag."""
        monkeypatch.delenv("HERALD_JOB_ID", raising=False)
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "notify"], returncode=0, stdout="", stderr=""
        )
        assert _send_via_herald("hello") is True
        mock_run.assert_called_once_with(
            ["herald", "notify", "--parse-mode", "plain", "--strict", "--message", "hello"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    @patch("notifier.subprocess.run")
    def test_send_with_job_id_routes_reply(self, mock_run, monkeypatch):
        """Herald-triggered run: HERALD_JOB_ID is passed through as --job."""
        monkeypatch.setenv("HERALD_JOB_ID", "01M0173FM7DVJ2Y9ZAEE1AB70M")
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "notify"], returncode=0, stdout="", stderr=""
        )
        assert _send_via_herald("hello") is True
        mock_run.assert_called_once_with(
            [
                "herald", "notify", "--parse-mode", "plain", "--strict",
                "--message", "hello", "--job", "01M0173FM7DVJ2Y9ZAEE1AB70M",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    @patch("notifier.subprocess.run")
    def test_empty_job_id_is_ignored(self, mock_run, monkeypatch):
        """An empty HERALD_JOB_ID must not produce `--job ""`."""
        monkeypatch.setenv("HERALD_JOB_ID", "")
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "notify"], returncode=0, stdout="", stderr=""
        )
        assert _send_via_herald("hello") is True
        assert "--job" not in mock_run.call_args[0][0]

    @patch("notifier.subprocess.run")
    def test_whitespace_job_id_is_ignored(self, mock_run, monkeypatch):
        """A blank-but-not-empty job id would be forwarded and misroute the reply."""
        monkeypatch.setenv("HERALD_JOB_ID", "   ")
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "notify"], returncode=0, stdout="", stderr=""
        )
        assert _send_via_herald("hello") is True
        assert "--job" not in mock_run.call_args[0][0]

    @patch("notifier.subprocess.run")
    def test_strict_is_always_passed(self, mock_run, monkeypatch):
        """Without --strict, Herald exits 0 even when it delivered nothing."""
        monkeypatch.delenv("HERALD_JOB_ID", raising=False)
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "notify"], returncode=0, stdout="", stderr=""
        )
        _send_via_herald("hello")
        assert "--strict" in mock_run.call_args[0][0]

    @patch("notifier.subprocess.run")
    def test_job_id_logged(self, mock_run, monkeypatch, caplog):
        """The job id appears in the log so routing is diagnosable."""
        monkeypatch.setenv("HERALD_JOB_ID", "01M0173FM7DVJ2Y9ZAEE1AB70M")
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "notify"], returncode=0, stdout="", stderr=""
        )
        with caplog.at_level("INFO", logger="notifier"):
            assert _send_via_herald("hello") is True
        assert "job 01M0173FM7DVJ2Y9ZAEE1AB70M" in caplog.text

    @patch("notifier.subprocess.run")
    def test_nonzero_exit(self, mock_run, monkeypatch):
        """Herald exits 1 when it could not deliver — report that as failure."""
        monkeypatch.delenv("HERALD_JOB_ID", raising=False)
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "notify"], returncode=1, stdout="", stderr="unknown job"
        )
        assert _send_via_herald("hello") is False

    @patch("notifier.subprocess.run", side_effect=FileNotFoundError)
    def test_binary_not_found(self, mock_run):
        assert _send_via_herald("hello") is False

    @patch("notifier.subprocess.run", side_effect=subprocess.TimeoutExpired("herald", 30))
    def test_timeout(self, mock_run):
        assert _send_via_herald("hello") is False

    @patch("notifier.subprocess.run", side_effect=OSError("permission denied"))
    def test_os_error(self, mock_run):
        assert _send_via_herald("hello") is False


class TestSendRunNotification:
    """Tests for send_run_notification() — integration."""

    @patch("notifier._herald_available", return_value=False)
    def test_skips_when_herald_not_installed(self, mock_avail):
        assert send_run_notification([]) is False

    @patch("notifier._herald_version_supported", return_value=False)
    @patch("notifier._herald_available", return_value=True)
    def test_skips_when_herald_too_old(self, mock_avail, mock_version):
        """Old Herald: available but version guard blocks send."""
        assert send_run_notification([]) is False

    @patch("notifier._send_via_herald", return_value=True)
    @patch("notifier._herald_version_supported", return_value=True)
    @patch("notifier._herald_available", return_value=True)
    def test_sends_when_herald_available_and_supported(self, mock_avail, mock_version, mock_send, monkeypatch):
        monkeypatch.setenv("RUNNER", "test")
        results = [{"name": "MKBHD", "new_episodes": 1, "failed": 0}]
        assert send_run_notification(results, elapsed_secs=60, status="success") is True
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "MKBHD" in msg

    @patch("notifier._send_via_herald", return_value=False)
    @patch("notifier._herald_version_supported", return_value=True)
    @patch("notifier._herald_available", return_value=True)
    def test_returns_false_when_send_fails(self, mock_avail, mock_version, mock_send, monkeypatch):
        monkeypatch.setenv("RUNNER", "test")
        assert send_run_notification([], elapsed_secs=5, status="success") is False

    @patch("notifier._send_via_herald")
    @patch("notifier._herald_version_supported", return_value=False)
    @patch("notifier._herald_available", return_value=True)
    def test_no_send_attempted_when_version_too_old(self, mock_avail, mock_version, mock_send, monkeypatch):
        """Verify no subprocess call to herald notify when version is too old."""
        monkeypatch.setenv("RUNNER", "test")
        send_run_notification([{"name": "X", "new_episodes": 1}], elapsed_secs=10)
        mock_send.assert_not_called()
