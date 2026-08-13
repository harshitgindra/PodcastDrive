"""Tests for src/notifier.py — Herald-based run notifications."""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from notifier import (
    send_run_notification,
    _format_message,
    _herald_available,
    _herald_supports_message_flag,
    _send_via_herald,
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
        assert "✅ 2 downloaded, 0 failed" in msg

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
        assert "0 downloaded, 0 failed" in msg

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


class TestHeraldSupportsMessageFlag:
    """Tests for _herald_supports_message_flag() — version guard."""

    @patch("notifier.subprocess.run")
    def test_version_0_2_0_supported(self, mock_run):
        """Boundary: exactly 0.2.0 is supported (inclusive)."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="0.2.0\n", stderr=""
        )
        assert _herald_supports_message_flag() is True

    @patch("notifier.subprocess.run")
    def test_version_0_3_1_supported(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="0.3.1\n", stderr=""
        )
        assert _herald_supports_message_flag() is True

    @patch("notifier.subprocess.run")
    def test_version_1_0_0_supported(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="1.0.0\n", stderr=""
        )
        assert _herald_supports_message_flag() is True

    @patch("notifier.subprocess.run")
    def test_version_0_1_0_not_supported(self, mock_run):
        """Old version: returns False, no exception."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="0.1.0\n", stderr=""
        )
        assert _herald_supports_message_flag() is False

    @patch("notifier.subprocess.run")
    def test_garbage_output(self, mock_run):
        """Non-version output: returns False, no exception."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="not a version\n", stderr=""
        )
        assert _herald_supports_message_flag() is False

    @patch("notifier.subprocess.run")
    def test_empty_output(self, mock_run):
        """Empty output: returns False, no exception."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="", stderr=""
        )
        assert _herald_supports_message_flag() is False

    @patch("notifier.subprocess.run")
    def test_nonzero_exit(self, mock_run):
        """Herald version exits non-zero: returns False."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=1, stdout="", stderr="error"
        )
        assert _herald_supports_message_flag() is False

    @patch("notifier.subprocess.run", side_effect=FileNotFoundError)
    def test_file_not_found(self, mock_run):
        """Herald not on PATH during version check: returns False, no raise."""
        assert _herald_supports_message_flag() is False

    @patch("notifier.subprocess.run", side_effect=subprocess.TimeoutExpired("herald", 10))
    def test_timeout(self, mock_run):
        """Version check times out: returns False, no raise."""
        assert _herald_supports_message_flag() is False

    @patch("notifier.subprocess.run")
    def test_version_check_calls_correct_command(self, mock_run):
        """Verifies the exact subprocess call."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "version"], returncode=0, stdout="0.2.0\n", stderr=""
        )
        _herald_supports_message_flag()
        mock_run.assert_called_once_with(
            ["herald", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )


class TestSendViaHerald:
    """Tests for _send_via_herald() — subprocess call with --message flag."""

    @patch("notifier.subprocess.run")
    def test_successful_send(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "notify", "--message", "msg"], returncode=0, stdout="", stderr=""
        )
        assert _send_via_herald("hello") is True
        mock_run.assert_called_once_with(
            ["herald", "notify", "--message", "hello"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    @patch("notifier.subprocess.run")
    def test_nonzero_exit(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["herald", "notify", "--message", "msg"], returncode=1, stdout="", stderr="error"
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

    @patch("notifier._herald_supports_message_flag", return_value=False)
    @patch("notifier._herald_available", return_value=True)
    def test_skips_when_herald_too_old(self, mock_avail, mock_version):
        """Old Herald: available but version guard blocks send."""
        assert send_run_notification([]) is False

    @patch("notifier._send_via_herald", return_value=True)
    @patch("notifier._herald_supports_message_flag", return_value=True)
    @patch("notifier._herald_available", return_value=True)
    def test_sends_when_herald_available_and_supported(
        self, mock_avail, mock_version, mock_send, monkeypatch
    ):
        monkeypatch.setenv("RUNNER", "test")
        results = [{"name": "MKBHD", "new_episodes": 1, "failed": 0}]
        assert send_run_notification(results, elapsed_secs=60, status="success") is True
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "MKBHD" in msg

    @patch("notifier._send_via_herald", return_value=False)
    @patch("notifier._herald_supports_message_flag", return_value=True)
    @patch("notifier._herald_available", return_value=True)
    def test_returns_false_when_send_fails(self, mock_avail, mock_version, mock_send, monkeypatch):
        monkeypatch.setenv("RUNNER", "test")
        assert send_run_notification([], elapsed_secs=5, status="success") is False

    @patch("notifier._send_via_herald")
    @patch("notifier._herald_supports_message_flag", return_value=False)
    @patch("notifier._herald_available", return_value=True)
    def test_no_send_attempted_when_version_too_old(
        self, mock_avail, mock_version, mock_send, monkeypatch
    ):
        """Verify no subprocess call to herald notify when version is too old."""
        monkeypatch.setenv("RUNNER", "test")
        send_run_notification([{"name": "X", "new_episodes": 1}], elapsed_secs=10)
        mock_send.assert_not_called()
