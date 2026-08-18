"""Additional CLI tests to cover missing lines."""

from unittest.mock import MagicMock, patch

import pytest

from mediasync.cli import _check_health, _run_migration, _run_regenerate_playlists, main
from mediasync.config import Config, Profile
from mediasync.pipeline import RunStats


@pytest.fixture
def onedrive_config(tmp_path):
    return Config(
        notion_token="tok",
        notion_database_id="db",
        profiles=[Profile("test")],
        storage_backend="onedrive",
        onedrive_client_id="cid",
        onedrive_client_secret="cs",
        onedrive_refresh_token="rt",
        onedrive_prefix="MediaSync",
        output_dir=str(tmp_path),
        herald_enabled=False,
    )


@pytest.fixture
def s3_config():
    return Config(
        notion_token="tok",
        notion_database_id="db",
        profiles=[Profile("test")],
        storage_backend="s3",
        s3_bucket="my-bucket",
        s3_region="us-west-2",
        s3_prefix="MediaSync",
        herald_enabled=False,
    )


class TestRunMigration:
    @patch("mediasync.migrate.migrate")
    def test_migration_success(self, mock_migrate, onedrive_config, capsys):
        mock_migrate.return_value = {"moved": 3, "skipped": 1, "failed": 0, "playlists": 2}
        result = _run_migration(onedrive_config, dry_run=False)
        assert result == 0
        output = capsys.readouterr().out
        assert "Moved:     3" in output
        assert "Playlists: 2" in output

    @patch("mediasync.migrate.migrate")
    def test_migration_with_failures(self, mock_migrate, onedrive_config, capsys):
        mock_migrate.return_value = {"moved": 1, "skipped": 0, "failed": 2, "playlists": 0}
        result = _run_migration(onedrive_config, dry_run=False)
        assert result == 1

    @patch("mediasync.migrate.migrate")
    def test_migration_dry_run(self, mock_migrate, onedrive_config, capsys):
        mock_migrate.return_value = {"moved": 0, "skipped": 5, "failed": 0, "playlists": 0}
        result = _run_migration(onedrive_config, dry_run=True)
        assert result == 0
        output = capsys.readouterr().out
        assert "DRY RUN" in output


class TestRunRegeneratePlaylists:
    @patch("mediasync.migrate.regenerate_playlists")
    def test_regenerate(self, mock_regen, onedrive_config, capsys):
        mock_regen.return_value = 3
        result = _run_regenerate_playlists(onedrive_config)
        assert result == 0
        output = capsys.readouterr().out
        assert "Regenerated 3 playlists" in output


class TestCheckHealth:
    @patch("mediasync.onedrive_client.OneDriveClient")
    def test_onedrive_healthy(self, MockClient, onedrive_config, capsys):
        mock_client = MockClient.return_value
        mock_client.check_health.return_value = True
        result = _check_health(onedrive_config)
        assert result == 0
        assert "OK" in capsys.readouterr().out

    @patch("mediasync.onedrive_client.OneDriveClient")
    def test_onedrive_unhealthy(self, MockClient, onedrive_config, capsys):
        mock_client = MockClient.return_value
        mock_client.check_health.return_value = False
        result = _check_health(onedrive_config)
        assert result == 1
        assert "FAIL" in capsys.readouterr().out

    def test_onedrive_init_failure(self, onedrive_config, capsys):
        from mediasync.onedrive_client import OneDriveError
        with patch("mediasync.onedrive_client.OneDriveClient", side_effect=OneDriveError("token expired")):
            result = _check_health(onedrive_config)
        assert result == 1
        assert "Token refresh failed" in capsys.readouterr().out

    @patch("mediasync.s3_client.boto3")
    def test_s3_healthy(self, mock_boto, s3_config, capsys):
        mock_client = MagicMock()
        mock_boto.client.return_value = mock_client
        result = _check_health(s3_config)
        assert result == 0
        assert "OK" in capsys.readouterr().out

    @patch("mediasync.s3_client.boto3")
    def test_s3_failure(self, mock_boto, s3_config, capsys):
        mock_client = MagicMock()
        mock_boto.client.return_value = mock_client
        mock_client.head_bucket.side_effect = Exception("access denied")
        result = _check_health(s3_config)
        assert result == 1
        assert "FAIL" in capsys.readouterr().out

    def test_unknown_backend(self, capsys):
        config = Config(
            notion_token="tok",
            notion_database_id="db",
            profiles=[Profile("test")],
            storage_backend="gcs",
            herald_enabled=False,
        )
        result = _check_health(config)
        assert result == 1
        assert "Unknown" in capsys.readouterr().out


class TestMainMigrateFlag:
    @patch("mediasync.cli._run_migration", return_value=0)
    @patch("mediasync.cli.Config.from_env")
    def test_migrate_flag(self, mock_env, mock_run_mig):
        mock_env.return_value = Config(
            notion_token="t", notion_database_id="d",
            profiles=[Profile("x")], herald_enabled=False,
        )
        assert main(["--migrate"]) == 0
        mock_run_mig.assert_called_once()

    @patch("mediasync.cli._run_regenerate_playlists", return_value=0)
    @patch("mediasync.cli.Config.from_env")
    def test_regenerate_flag(self, mock_env, mock_regen):
        mock_env.return_value = Config(
            notion_token="t", notion_database_id="d",
            profiles=[Profile("x")], herald_enabled=False,
        )
        assert main(["--regenerate-playlists"]) == 0
        mock_regen.assert_called_once()

    @patch("mediasync.cli._check_health", return_value=0)
    @patch("mediasync.cli.Config.from_env")
    def test_check_flag(self, mock_env, mock_check):
        mock_env.return_value = Config(
            notion_token="t", notion_database_id="d",
            profiles=[Profile("x")], herald_enabled=False,
        )
        assert main(["--check"]) == 0
        mock_check.assert_called_once()

    @patch("mediasync.cli._reset")
    @patch("mediasync.cli._notify")
    @patch("mediasync.cli.run")
    @patch("mediasync.cli.Config.from_env")
    def test_reset_flag(self, mock_env, mock_run, mock_notify, mock_reset):
        cfg = Config(
            notion_token="t", notion_database_id="d",
            profiles=[Profile("x")], herald_enabled=False,
        )
        mock_env.return_value = cfg
        mock_run.return_value = RunStats()
        assert main(["--reset"]) == 0
        mock_reset.assert_called_once_with(cfg)
