"""Unit tests for src/preflight.py."""

import sys
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from preflight import (
    _check_aws_credentials,
    _check_cloudfront,
    _check_env_vars,
    _check_ffmpeg,
    _check_notion,
    _check_s3_bucket,
    _check_yt_dlp,
    _fail,
    _ok,
    _warn,
    _section,
    run_preflight,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _client_error(code: str, message: str = "error") -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": message}}, "op"
    )


# ── colour helpers ────────────────────────────────────────────────────────────

class TestHelpers:
    def test_ok_prints(self, capsys):
        _ok("all good")
        assert "all good" in capsys.readouterr().out

    def test_warn_prints(self, capsys):
        _warn("watch out")
        assert "watch out" in capsys.readouterr().out

    def test_section_prints(self, capsys):
        _section("My Section")
        assert "My Section" in capsys.readouterr().out

    def test_fail_exits_with_1(self):
        with pytest.raises(SystemExit) as exc_info:
            _fail("something broke")
        assert exc_info.value.code == 1

    def test_fail_prints_message(self, capsys):
        with pytest.raises(SystemExit):
            _fail("critical error")
        assert "critical error" in capsys.readouterr().out


# ── _check_env_vars ───────────────────────────────────────────────────────────

class TestCheckEnvVars:
    _FULL_ENV = {
        "S3_BUCKET": "my-bucket",
        "CLOUDFRONT_BASE": "https://cdn.example.com",
        "CLOUDFRONT_DISTRIBUTION_ID": "E123ABC",
        "AWS_DEFAULT_REGION": "us-west-2",
    }

    def test_passes_when_all_vars_set(self, capsys):
        with patch.dict("os.environ", self._FULL_ENV, clear=True):
            _check_env_vars()  # should not raise
        out = capsys.readouterr().out
        assert "S3_BUCKET" in out
        assert "CLOUDFRONT_BASE" in out

    def test_fails_when_s3_bucket_missing(self):
        env = {**self._FULL_ENV, "S3_BUCKET": ""}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit):
                _check_env_vars()

    def test_fails_when_cloudfront_base_missing(self):
        env = {**self._FULL_ENV, "CLOUDFRONT_BASE": ""}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit):
                _check_env_vars()

    def test_fails_when_distribution_id_missing(self):
        env = {**self._FULL_ENV, "CLOUDFRONT_DISTRIBUTION_ID": ""}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit):
                _check_env_vars()

    def test_warns_when_region_missing(self, capsys):
        env = {k: v for k, v in self._FULL_ENV.items() if k != "AWS_DEFAULT_REGION"}
        with patch.dict("os.environ", env, clear=True):
            _check_env_vars()
        out = capsys.readouterr().out
        assert "defaulting to us-west-2" in out

    def test_sets_default_region_when_missing(self):
        env = {k: v for k, v in self._FULL_ENV.items() if k != "AWS_DEFAULT_REGION"}
        with patch.dict("os.environ", env, clear=True):
            _check_env_vars()
            import os
            assert os.environ.get("AWS_DEFAULT_REGION") == "us-west-2"

    def test_ok_when_region_present(self, capsys):
        with patch.dict("os.environ", self._FULL_ENV, clear=True):
            _check_env_vars()
        assert "us-west-2" in capsys.readouterr().out


# ── _check_aws_credentials ────────────────────────────────────────────────────

class TestCheckAwsCredentials:
    def _mock_session(self, account="123456789012", region="us-west-2"):
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": account}
        mock_session = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_session.region_name = region
        return mock_session

    def test_returns_region_on_success(self, capsys):
        with patch("boto3.session.Session", return_value=self._mock_session()):
            region = _check_aws_credentials()
        assert region == "us-west-2"
        assert "123456789012" in capsys.readouterr().out

    def test_uses_env_region_when_session_has_none(self):
        session = self._mock_session(region=None)
        with patch("boto3.session.Session", return_value=session), \
             patch.dict("os.environ", {"AWS_DEFAULT_REGION": "eu-central-1"}):
            region = _check_aws_credentials()
        assert region == "eu-central-1"

    def test_warns_on_unusual_region(self, capsys):
        session = self._mock_session(region="bad")
        with patch("boto3.session.Session", return_value=session):
            _check_aws_credentials()
        assert "looks unusual" in capsys.readouterr().out

    def test_fails_on_no_credentials(self):
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.side_effect = botocore.exceptions.NoCredentialsError()
        mock_session.client.return_value = mock_sts
        with patch("boto3.session.Session", return_value=mock_session):
            with pytest.raises(SystemExit):
                _check_aws_credentials()

    def test_fails_on_client_error(self):
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.side_effect = _client_error("AccessDenied")
        mock_session.client.return_value = mock_sts
        with patch("boto3.session.Session", return_value=mock_session):
            with pytest.raises(SystemExit):
                _check_aws_credentials()


# ── _check_s3_bucket ──────────────────────────────────────────────────────────

class TestCheckS3Bucket:
    def test_passes_when_bucket_accessible(self, capsys):
        mock_s3 = MagicMock()
        mock_s3.head_bucket.return_value = {}
        mock_s3.list_objects_v2.return_value = {}
        with patch("boto3.client", return_value=mock_s3), \
             patch.dict("os.environ", {"S3_BUCKET": "my-bucket"}):
            _check_s3_bucket("us-west-2", dry_run=False)
        out = capsys.readouterr().out
        assert "accessible" in out
        assert "list access confirmed" in out

    def test_dry_run_skips_list_check(self):
        mock_s3 = MagicMock()
        mock_s3.head_bucket.return_value = {}
        with patch("boto3.client", return_value=mock_s3), \
             patch.dict("os.environ", {"S3_BUCKET": "my-bucket"}):
            _check_s3_bucket("us-west-2", dry_run=True)
        mock_s3.list_objects_v2.assert_not_called()

    def test_fails_on_404(self):
        mock_s3 = MagicMock()
        mock_s3.head_bucket.side_effect = _client_error("404")
        with patch("boto3.client", return_value=mock_s3), \
             patch.dict("os.environ", {"S3_BUCKET": "missing-bucket"}):
            with pytest.raises(SystemExit):
                _check_s3_bucket("us-west-2", dry_run=False)

    def test_fails_on_no_such_bucket(self):
        mock_s3 = MagicMock()
        mock_s3.head_bucket.side_effect = _client_error("NoSuchBucket")
        with patch("boto3.client", return_value=mock_s3), \
             patch.dict("os.environ", {"S3_BUCKET": "missing-bucket"}):
            with pytest.raises(SystemExit):
                _check_s3_bucket("us-west-2", dry_run=False)

    def test_fails_on_403(self):
        mock_s3 = MagicMock()
        mock_s3.head_bucket.side_effect = _client_error("403")
        with patch("boto3.client", return_value=mock_s3), \
             patch.dict("os.environ", {"S3_BUCKET": "forbidden-bucket"}):
            with pytest.raises(SystemExit):
                _check_s3_bucket("us-west-2", dry_run=False)

    def test_fails_on_other_client_error(self):
        mock_s3 = MagicMock()
        mock_s3.head_bucket.side_effect = _client_error("InternalError")
        with patch("boto3.client", return_value=mock_s3), \
             patch.dict("os.environ", {"S3_BUCKET": "my-bucket"}):
            with pytest.raises(SystemExit):
                _check_s3_bucket("us-west-2", dry_run=False)

    def test_fails_on_list_access_denied(self):
        mock_s3 = MagicMock()
        mock_s3.head_bucket.return_value = {}
        mock_s3.list_objects_v2.side_effect = _client_error("AccessDenied")
        with patch("boto3.client", return_value=mock_s3), \
             patch.dict("os.environ", {"S3_BUCKET": "my-bucket"}):
            with pytest.raises(SystemExit):
                _check_s3_bucket("us-west-2", dry_run=False)


# ── _check_cloudfront ─────────────────────────────────────────────────────────

class TestCheckCloudfront:
    def _mock_cf(self, status="Deployed", domain="d123.cloudfront.net"):
        mock_cf = MagicMock()
        mock_cf.get_distribution.return_value = {
            "Distribution": {"Status": status, "DomainName": domain}
        }
        return mock_cf

    def test_passes_when_deployed(self, capsys):
        with patch("boto3.client", return_value=self._mock_cf()), \
             patch.dict("os.environ", {"CLOUDFRONT_DISTRIBUTION_ID": "E123"}):
            _check_cloudfront("us-west-2")
        assert "Deployed" in capsys.readouterr().out

    def test_warns_when_not_deployed(self, capsys):
        with patch("boto3.client", return_value=self._mock_cf(status="InProgress")), \
             patch.dict("os.environ", {"CLOUDFRONT_DISTRIBUTION_ID": "E123"}):
            _check_cloudfront("us-west-2")
        assert "InProgress" in capsys.readouterr().out

    def test_fails_when_no_dist_id(self):
        with patch("boto3.client", return_value=self._mock_cf()), \
             patch.dict("os.environ", {"CLOUDFRONT_DISTRIBUTION_ID": ""}):
            with pytest.raises(SystemExit):
                _check_cloudfront("us-west-2")

    def test_fails_on_no_such_distribution(self):
        mock_cf = MagicMock()
        mock_cf.get_distribution.side_effect = _client_error("NoSuchDistribution")
        with patch("boto3.client", return_value=mock_cf), \
             patch.dict("os.environ", {"CLOUDFRONT_DISTRIBUTION_ID": "EBAD"}):
            with pytest.raises(SystemExit):
                _check_cloudfront("us-west-2")

    def test_fails_on_access_denied(self):
        mock_cf = MagicMock()
        mock_cf.get_distribution.side_effect = _client_error("AccessDenied")
        with patch("boto3.client", return_value=mock_cf), \
             patch.dict("os.environ", {"CLOUDFRONT_DISTRIBUTION_ID": "E123"}):
            with pytest.raises(SystemExit):
                _check_cloudfront("us-west-2")

    def test_fails_on_other_client_error(self):
        mock_cf = MagicMock()
        mock_cf.get_distribution.side_effect = _client_error("InternalError")
        with patch("boto3.client", return_value=mock_cf), \
             patch.dict("os.environ", {"CLOUDFRONT_DISTRIBUTION_ID": "E123"}):
            with pytest.raises(SystemExit):
                _check_cloudfront("us-west-2")


# ── _check_yt_dlp ─────────────────────────────────────────────────────────────

class TestCheckYtDlp:
    def test_passes_when_installed_and_binary_works(self, capsys):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "2024.01.01\n"
        with patch("subprocess.run", return_value=mock_result), \
             patch.dict(sys.modules, {"yt_dlp": MagicMock()}):
            _check_yt_dlp()
        out = capsys.readouterr().out
        assert "importable" in out
        assert "2024.01.01" in out

    def test_fails_when_import_fails(self):
        with patch.dict(sys.modules, {"yt_dlp": None}):
            # Remove yt_dlp from modules to simulate ImportError
            with patch("builtins.__import__", side_effect=ImportError("no module")):
                with pytest.raises(SystemExit):
                    _check_yt_dlp()

    def test_fails_when_binary_returns_nonzero(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result), \
             patch.dict(sys.modules, {"yt_dlp": MagicMock()}):
            with pytest.raises(SystemExit):
                _check_yt_dlp()


# ── _check_ffmpeg ─────────────────────────────────────────────────────────────

class TestCheckFfmpeg:
    def test_passes_when_ffmpeg_available(self, capsys):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ffmpeg version 6.0\nmore lines"
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", return_value=mock_result):
            _check_ffmpeg()
        assert "ffmpeg version 6.0" in capsys.readouterr().out

    def test_fails_when_ffmpeg_not_on_path(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(SystemExit):
                _check_ffmpeg()

    def test_fails_when_ffmpeg_returns_nonzero(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", return_value=mock_result):
            with pytest.raises(SystemExit):
                _check_ffmpeg()

    def test_handles_empty_stdout(self, capsys):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", return_value=mock_result):
            _check_ffmpeg()
        assert "unknown" in capsys.readouterr().out


# ── _check_notion ─────────────────────────────────────────────────────────────

class TestCheckNotion:
    _ENV = {
        "NOTION_API_KEY": "secret_realkey123",
        "NOTION_DATABASE_ID": "real-db-id",
    }

    def test_passes_when_api_reachable(self, capsys):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.dict("os.environ", self._ENV):
            _check_notion()
        assert "valid" in capsys.readouterr().out

    def test_fails_when_api_key_missing(self):
        with patch.dict("os.environ", {**self._ENV, "NOTION_API_KEY": ""}):
            with pytest.raises(SystemExit):
                _check_notion()

    def test_fails_when_api_key_is_placeholder(self):
        with patch.dict("os.environ", {**self._ENV, "NOTION_API_KEY": "secret_xxx123"}):
            with pytest.raises(SystemExit):
                _check_notion()

    def test_fails_when_db_id_missing(self):
        with patch.dict("os.environ", {**self._ENV, "NOTION_DATABASE_ID": ""}):
            with pytest.raises(SystemExit):
                _check_notion()

    def test_fails_when_db_id_is_placeholder(self):
        with patch.dict("os.environ", {**self._ENV, "NOTION_DATABASE_ID": "xxx-fake"}):
            with pytest.raises(SystemExit):
                _check_notion()

    def test_fails_on_401(self):
        import urllib.error
        err = urllib.error.HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=err), \
             patch.dict("os.environ", self._ENV):
            with pytest.raises(SystemExit):
                _check_notion()

    def test_fails_on_404(self):
        import urllib.error
        err = urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=err), \
             patch.dict("os.environ", self._ENV):
            with pytest.raises(SystemExit):
                _check_notion()

    def test_fails_on_other_http_error(self):
        import urllib.error
        err = urllib.error.HTTPError(url="", code=500, msg="Server Error", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=err), \
             patch.dict("os.environ", self._ENV):
            with pytest.raises(SystemExit):
                _check_notion()

    def test_fails_on_connection_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")), \
             patch.dict("os.environ", self._ENV):
            with pytest.raises(SystemExit):
                _check_notion()


# ── run_preflight ─────────────────────────────────────────────────────────────

class TestRunPreflight:
    """Integration-style tests for the run_preflight() orchestrator."""

    def _patch_all_checks(self):
        """Return a dict of patches that make all individual checks pass."""
        return {
            "_check_env_vars": patch("preflight._check_env_vars"),
            "_check_aws_credentials": patch("preflight._check_aws_credentials", return_value="us-west-2"),
            "_check_s3_bucket": patch("preflight._check_s3_bucket"),
            "_check_cloudfront": patch("preflight._check_cloudfront"),
            "_check_yt_dlp": patch("preflight._check_yt_dlp"),
            "_check_ffmpeg": patch("preflight._check_ffmpeg"),
            "_check_notion": patch("preflight._check_notion"),
        }

    def test_runs_all_checks_in_normal_mode(self):
        patches = self._patch_all_checks()
        mocks = {k: p.start() for k, p in patches.items()}
        try:
            with patch.dict("os.environ", {"CONFIG_PROVIDER": "yaml"}):
                run_preflight(dry_run=False)
            mocks["_check_env_vars"].assert_called_once()
            mocks["_check_aws_credentials"].assert_called_once()
            mocks["_check_s3_bucket"].assert_called_once_with("us-west-2", dry_run=False)
            mocks["_check_cloudfront"].assert_called_once_with("us-west-2")
            mocks["_check_yt_dlp"].assert_called_once()
            mocks["_check_ffmpeg"].assert_called_once()
            mocks["_check_notion"].assert_not_called()
        finally:
            for p in patches.values():
                p.stop()

    def test_calls_notion_check_when_provider_is_notion(self):
        patches = self._patch_all_checks()
        mocks = {k: p.start() for k, p in patches.items()}
        try:
            with patch.dict("os.environ", {"CONFIG_PROVIDER": "notion"}):
                run_preflight(dry_run=False)
            mocks["_check_notion"].assert_called_once()
        finally:
            for p in patches.values():
                p.stop()

    def test_passes_dry_run_to_s3_check(self):
        patches = self._patch_all_checks()
        mocks = {k: p.start() for k, p in patches.items()}
        try:
            with patch.dict("os.environ", {"CONFIG_PROVIDER": "yaml"}):
                run_preflight(dry_run=True)
            mocks["_check_s3_bucket"].assert_called_once_with("us-west-2", dry_run=True)
        finally:
            for p in patches.values():
                p.stop()

    def test_prints_dry_run_notice(self, capsys):
        patches = self._patch_all_checks()
        for p in patches.values():
            p.start()
        try:
            with patch.dict("os.environ", {"CONFIG_PROVIDER": "yaml"}):
                run_preflight(dry_run=True)
            assert "dry-run mode" in capsys.readouterr().out
        finally:
            for p in patches.values():
                p.stop()

    def test_prints_success_message(self, capsys):
        patches = self._patch_all_checks()
        for p in patches.values():
            p.start()
        try:
            with patch.dict("os.environ", {"CONFIG_PROVIDER": "yaml"}):
                run_preflight()
            assert "All preflight checks passed" in capsys.readouterr().out
        finally:
            for p in patches.values():
                p.stop()
