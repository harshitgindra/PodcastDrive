"""Tests for mediasync.cli module."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mediasync.cli import _notify, main
from mediasync.config import Config, Profile
from mediasync.pipeline import RunStats


@pytest.fixture
def mock_config():
    return Config(
        notion_token="tok",
        notion_database_id="db",
        s3_bucket="hg-mediafiles",
        s3_region="us-west-2",
        s3_prefix="MediaSync",
        profiles=[Profile("test")],
        herald_enabled=False,
        herald_job_id="mediasync",
    )


class TestMain:
    @patch("mediasync.cli.Config.from_env")
    def test_config_error_returns_1(self, mock_from_env):
        mock_from_env.side_effect = ValueError("missing MEDIASYNC_NOTION_TOKEN")
        assert main([]) == 1

    @patch("mediasync.cli._notify")
    @patch("mediasync.cli.run")
    @patch("mediasync.cli.Config.from_env")
    def test_successful_run_returns_0(self, mock_env, mock_run, mock_notify, mock_config):
        mock_env.return_value = mock_config
        mock_run.return_value = RunStats(processed=2, failed=0, deleted=0, skipped=0)

        assert main([]) == 0
        mock_run.assert_called_once_with(mock_config)

    @patch("mediasync.cli._notify")
    @patch("mediasync.cli.run")
    @patch("mediasync.cli.Config.from_env")
    def test_failures_return_1(self, mock_env, mock_run, mock_notify, mock_config):
        mock_env.return_value = mock_config
        mock_run.return_value = RunStats(processed=1, failed=2, deleted=0, skipped=0)

        assert main([]) == 1

    @patch("mediasync.notion_client.NotionClient")
    @patch("mediasync.cli.Config.from_env")
    def test_dry_run(self, mock_env, MockNotion, mock_config, capsys):
        mock_env.return_value = mock_config
        mock_notion = MockNotion.return_value
        mock_notion.get_pending.return_value = []
        mock_notion.get_deletions.return_value = []

        result = main(["--dry-run"])

        assert result == 0
        output = capsys.readouterr().out
        assert "Pending downloads: 0" in output


class TestDryRunOutput:
    """Test dry-run with actual entries."""

    @patch("mediasync.notion_client.NotionClient")
    @patch("mediasync.cli.Config.from_env")
    def test_dry_run_with_entries(self, mock_env, MockNotion, mock_config, capsys):
        from mediasync.notion_client import Format, MediaEntry, Status

        mock_env.return_value = mock_config
        mock_notion = MockNotion.return_value
        mock_notion.get_pending.return_value = [
            MediaEntry("p1", "https://youtube.com/watch?v=x", "Harshit", Format.AUDIO, Status.PENDING, False),
        ]
        mock_notion.get_deletions.return_value = [
            MediaEntry("p2", "https://youtube.com/watch?v=y", "Dishita", Format.VIDEO, Status.DONE, True, "MediaSync/Dishita/video/f.mp4"),
        ]

        result = main(["--dry-run"])

        assert result == 0
        output = capsys.readouterr().out
        assert "[Harshit] audio:" in output
        assert "[Dishita]" in output
        assert "MediaSync/Dishita/video/f.mp4" in output


class TestNotify:
    def test_herald_disabled_skips(self):
        config = Config(
            notion_token="t", notion_database_id="d",
            s3_bucket="b", s3_region="r", s3_prefix="p",
            profiles=[Profile("x")],
            herald_enabled=False, herald_job_id="j",
        )
        _notify(config, RunStats(), 10)

    @staticmethod
    def _config(**kw):
        base = dict(
            notion_token="t", notion_database_id="d",
            s3_bucket="b", s3_region="r", s3_prefix="p",
            profiles=[Profile("x")],
            herald_enabled=True, herald_job_id="j",
        )
        base.update(kw)
        return Config(**base)

    @patch("shutil.which", return_value=None)
    def test_missing_herald_binary_warns_rather_than_vanishing(self, mock_which, caplog):
        """Herald's listen.services.<svc>.env.PATH replaces the child PATH instead of
        extending it, so a config missing ~/.local/bin drops every summary.  That used
        to be a bare `return` with no trace at all."""
        with caplog.at_level("WARNING", logger="mediasync.cli"):
            _notify(self._config(), RunStats(), 10)

        assert "herald binary not found" in caplog.text
        assert "PATH" in caplog.text

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/herald")
    def test_herald_called_with_stats(self, mock_which, mock_run):
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        config = self._config(herald_job_id="mediasync")
        stats = RunStats(processed=3, failed=1, deleted=2, skipped=0)
        _notify(config, stats, 65)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        # The resolved path is used, not a second PATH lookup by the child.
        assert cmd[0] == "/usr/local/bin/herald"
        assert "--job" in cmd
        assert cmd[cmd.index("--job") + 1] == "mediasync"
        msg_idx = cmd.index("--message") + 1
        message = cmd[msg_idx]
        assert "Processed: 3" in message
        assert "Failed: 1" in message

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/herald")
    def test_nonzero_exit_is_reported(self, mock_which, mock_run, caplog):
        """--strict makes herald exit non-zero when nothing was delivered; ignoring the
        return code meant an undelivered summary looked identical to a delivered one."""
        mock_run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="no recipients")
        with caplog.at_level("WARNING", logger="mediasync.cli"):
            _notify(self._config(), RunStats(processed=1), 10)

        assert "herald notify failed (exit 1)" in caplog.text
        assert "no recipients" in caplog.text

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/herald")
    def test_no_job_id_still_sends(self, mock_which, mock_run):
        """Without HERALD_JOB_ID the summary goes to the default recipient rather than
        being suppressed."""
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        _notify(self._config(herald_job_id=""), RunStats(processed=1), 10)

        cmd = mock_run.call_args[0][0]
        assert "--job" not in cmd


class TestNotifyException:
    @patch("subprocess.run", side_effect=OSError("herald crashed"))
    @patch("shutil.which", return_value="/usr/local/bin/herald")
    def test_subprocess_error_is_logged_not_raised(self, mock_which, mock_run, caplog):
        """A failed summary must not fail an otherwise successful run — but it must
        leave a trace, which it previously did not (`except Exception: pass`)."""
        config = Config(
            notion_token="t", notion_database_id="d",
            s3_bucket="b", s3_region="r", s3_prefix="p",
            profiles=[Profile("x")],
            herald_enabled=True, herald_job_id="j",
        )
        with caplog.at_level("WARNING", logger="mediasync.cli"):
            _notify(config, RunStats(processed=1), 10)
        assert "Could not run herald notify" in caplog.text

    @patch("subprocess.run", side_effect=OSError("herald crashed"))
    @patch("shutil.which", return_value="/usr/local/bin/herald")
    def test_subprocess_error_swallowed(self, mock_which, mock_run):
        config = Config(
            notion_token="t", notion_database_id="d",
            s3_bucket="b", s3_region="r", s3_prefix="p",
            profiles=[Profile("x")],
            herald_enabled=True, herald_job_id="j",
        )
        _notify(config, RunStats(processed=1), 10)
