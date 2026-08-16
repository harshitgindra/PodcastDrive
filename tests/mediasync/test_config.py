"""Tests for mediasync.config module."""

import os
import pytest
from unittest.mock import patch

from mediasync.config import Config, Profile


class TestConfigFromEnv:
    """Tests for Config.from_env()."""

    @pytest.fixture
    def full_env(self):
        """Complete set of required environment variables."""
        return {
            "MEDIASYNC_NOTION_TOKEN": "ntn_test123",
            "MEDIASYNC_NOTION_DATABASE_ID": "db_abc456",
            "MEDIASYNC_S3_BUCKET": "hg-mediafiles",
            "MEDIASYNC_PROFILES": "Harshit,Dishita",
        }

    def test_loads_required_fields(self, full_env):
        with patch.dict(os.environ, full_env, clear=True):
            config = Config.from_env()

        assert config.notion_token == "ntn_test123"
        assert config.notion_database_id == "db_abc456"
        assert config.s3_bucket == "hg-mediafiles"
        assert config.profiles == [Profile("Harshit"), Profile("Dishita")]

    def test_default_values(self, full_env):
        with patch.dict(os.environ, full_env, clear=True):
            config = Config.from_env()

        assert config.s3_region == "us-west-2"
        assert config.s3_prefix == "MediaSync"
        assert config.max_duration_secs == 7200
        assert config.output_dir == "/tmp/mediasync"
        assert config.herald_enabled is True
        assert config.herald_job_id == "mediasync"

    def test_custom_optional_values(self, full_env):
        full_env.update({
            "MEDIASYNC_S3_REGION": "us-east-1",
            "MEDIASYNC_S3_PREFIX": "MyMedia",
            "MEDIASYNC_MAX_DURATION_SECS": "3600",
            "MEDIASYNC_OUTPUT_DIR": "/data/tmp",
            "MEDIASYNC_HERALD_ENABLED": "false",
            "MEDIASYNC_HERALD_JOB_ID": "custom-job",
        })
        with patch.dict(os.environ, full_env, clear=True):
            config = Config.from_env()

        assert config.s3_region == "us-east-1"
        assert config.s3_prefix == "MyMedia"
        assert config.max_duration_secs == 3600
        assert config.output_dir == "/data/tmp"
        assert config.herald_enabled is False
        assert config.herald_job_id == "custom-job"

    def test_missing_notion_token_raises(self, full_env):
        del full_env["MEDIASYNC_NOTION_TOKEN"]
        with patch.dict(os.environ, full_env, clear=True):
            with pytest.raises(ValueError, match="MEDIASYNC_NOTION_TOKEN"):
                Config.from_env()

    def test_missing_notion_database_id_raises(self, full_env):
        del full_env["MEDIASYNC_NOTION_DATABASE_ID"]
        with patch.dict(os.environ, full_env, clear=True):
            with pytest.raises(ValueError, match="MEDIASYNC_NOTION_DATABASE_ID"):
                Config.from_env()

    def test_missing_s3_bucket_raises(self, full_env):
        del full_env["MEDIASYNC_S3_BUCKET"]
        with patch.dict(os.environ, full_env, clear=True):
            with pytest.raises(ValueError, match="MEDIASYNC_S3_BUCKET"):
                Config.from_env()

    def test_missing_profiles_raises(self, full_env):
        del full_env["MEDIASYNC_PROFILES"]
        with patch.dict(os.environ, full_env, clear=True):
            with pytest.raises(ValueError, match="MEDIASYNC_PROFILES"):
                Config.from_env()

    def test_profiles_whitespace_handling(self, full_env):
        full_env["MEDIASYNC_PROFILES"] = " alice , bob , "
        with patch.dict(os.environ, full_env, clear=True):
            config = Config.from_env()

        assert config.profiles == [Profile("alice"), Profile("bob")]

    def test_single_profile(self, full_env):
        full_env["MEDIASYNC_PROFILES"] = "solo"
        with patch.dict(os.environ, full_env, clear=True):
            config = Config.from_env()

        assert config.profiles == [Profile("solo")]

    def test_invalid_storage_backend_raises(self, full_env):
        full_env["MEDIASYNC_STORAGE"] = "gcs"
        with patch.dict(os.environ, full_env, clear=True):
            with pytest.raises(ValueError, match="'s3' or 'onedrive'"):
                Config.from_env()

    def test_onedrive_missing_client_id_raises(self, full_env):
        full_env["MEDIASYNC_STORAGE"] = "onedrive"
        full_env["MEDIASYNC_ONEDRIVE_CLIENT_SECRET"] = "sec"
        full_env["MEDIASYNC_ONEDRIVE_REFRESH_TOKEN"] = "rt"
        with patch.dict(os.environ, full_env, clear=True):
            with pytest.raises(ValueError, match="ONEDRIVE_CLIENT_ID"):
                Config.from_env()

    def test_onedrive_missing_client_secret_raises(self, full_env):
        full_env["MEDIASYNC_STORAGE"] = "onedrive"
        full_env["MEDIASYNC_ONEDRIVE_CLIENT_ID"] = "cid"
        full_env["MEDIASYNC_ONEDRIVE_REFRESH_TOKEN"] = "rt"
        with patch.dict(os.environ, full_env, clear=True):
            with pytest.raises(ValueError, match="ONEDRIVE_CLIENT_SECRET"):
                Config.from_env()

    def test_onedrive_missing_refresh_token_raises(self, full_env):
        full_env["MEDIASYNC_STORAGE"] = "onedrive"
        full_env["MEDIASYNC_ONEDRIVE_CLIENT_ID"] = "cid"
        full_env["MEDIASYNC_ONEDRIVE_CLIENT_SECRET"] = "sec"
        with patch.dict(os.environ, full_env, clear=True):
            with pytest.raises(ValueError, match="ONEDRIVE_REFRESH_TOKEN"):
                Config.from_env()

    def test_onedrive_valid_config(self, full_env):
        full_env["MEDIASYNC_STORAGE"] = "onedrive"
        full_env["MEDIASYNC_ONEDRIVE_CLIENT_ID"] = "cid"
        full_env["MEDIASYNC_ONEDRIVE_CLIENT_SECRET"] = "sec"
        full_env["MEDIASYNC_ONEDRIVE_REFRESH_TOKEN"] = "rt"
        with patch.dict(os.environ, full_env, clear=True):
            config = Config.from_env()
        assert config.storage_backend == "onedrive"
        assert config.prefix == "MediaSync"

    def test_prefix_property_s3(self, full_env):
        full_env["MEDIASYNC_S3_PREFIX"] = "Custom"
        with patch.dict(os.environ, full_env, clear=True):
            config = Config.from_env()
        assert config.prefix == "Custom"
