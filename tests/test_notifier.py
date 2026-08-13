"""Tests for src/notifier.py — Telegram run notifications."""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from notifier import send_run_notification, _send_telegram


class TestSendRunNotification:
    """Tests for send_run_notification()."""

    def test_skips_when_no_token(self, monkeypatch):
        """No-op when TELEGRAM_BOT_TOKEN is unset."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert send_run_notification([]) is False

    def test_skips_when_no_chat_id(self, monkeypatch):
        """No-op when TELEGRAM_CHAT_ID is unset."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert send_run_notification([]) is False

    @patch("notifier._send_telegram", return_value=True)
    def test_formats_success_message(self, mock_send, monkeypatch):
        """Formats a successful run with new episodes."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        monkeypatch.setenv("RUNNER", "test-mac")

        results = [
            {"name": "MKBHD", "new_episodes": 2, "failed": 0, "bot_detected": False},
            {"name": "Huberman", "new_episodes": 0, "failed": 0, "bot_detected": False},
        ]
        assert send_run_notification(results, elapsed_secs=134, status="success") is True

        msg = mock_send.call_args[0][2]
        assert "test-mac" in msg
        assert "2m 14s" in msg
        assert "MKBHD" in msg
        assert "2 new" in msg
        assert "Huberman" in msg
        assert "up to date" in msg
        assert "✅ 2 downloaded, 0 failed" in msg

    @patch("notifier._send_telegram", return_value=True)
    def test_formats_failure_message(self, mock_send, monkeypatch):
        """Formats a run with failures."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        monkeypatch.setenv("RUNNER", "ec2")

        results = [
            {"name": "ThinkSchool", "new_episodes": 0, "failed": 2, "bot_detected": False},
        ]
        assert send_run_notification(results, elapsed_secs=55, status="partial_failure") is True

        msg = mock_send.call_args[0][2]
        assert "ThinkSchool" in msg
        assert "2 failed" in msg
        assert "⚠️" in msg

    @patch("notifier._send_telegram", return_value=True)
    def test_formats_bot_detected(self, mock_send, monkeypatch):
        """Shows bot detection warning."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

        results = [{"name": "Lex", "new_episodes": 0, "failed": 0, "bot_detected": True}]
        assert send_run_notification(results, elapsed_secs=10, status="success") is True

        msg = mock_send.call_args[0][2]
        assert "bot detected" in msg

    @patch("notifier._send_telegram", return_value=True)
    def test_formats_error_message(self, mock_send, monkeypatch):
        """Shows error string when present."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

        results = [{"name": "Broken", "error": "Connection timeout"}]
        assert send_run_notification(results, elapsed_secs=5, status="failure") is True

        msg = mock_send.call_args[0][2]
        assert "Broken" in msg
        assert "Connection timeout" in msg

    @patch("notifier._send_telegram", return_value=True)
    def test_empty_results(self, mock_send, monkeypatch):
        """Handles empty results list gracefully."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

        assert send_run_notification([], elapsed_secs=3, status="success") is True
        msg = mock_send.call_args[0][2]
        assert "0 downloaded, 0 failed" in msg


class TestSendTelegram:
    """Tests for _send_telegram() HTTP call."""

    @patch("notifier.urllib.request.urlopen")
    def test_successful_send(self, mock_urlopen):
        """Returns True on successful HTTP call."""
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        assert _send_telegram("token", "123", "hello") is True

    @patch("notifier.urllib.request.urlopen")
    def test_sends_correct_payload(self, mock_urlopen):
        """Verifies correct URL and payload structure."""
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        _send_telegram("mytoken", "999", "test msg")

        req = mock_urlopen.call_args[0][0]
        assert "mytoken" in req.full_url
        payload = json.loads(req.data)
        assert payload["chat_id"] == "999"
        assert payload["text"] == "test msg"
        assert payload["parse_mode"] == "Markdown"

    @patch("notifier.urllib.request.urlopen", side_effect=OSError("network down"))
    def test_handles_network_error(self, mock_urlopen):
        """Returns False on network error without raising."""
        assert _send_telegram("token", "123", "hello") is False
