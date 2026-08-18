"""Tests for the shared AWS session configuration."""

import logging
import threading

import boto3
import pytest
from botocore.config import Config

import aws


@pytest.fixture(autouse=True)
def clean_session(monkeypatch):
    """Each test starts and ends without a shared session installed."""
    monkeypatch.delenv("AWS_CONNECT_TIMEOUT", raising=False)
    monkeypatch.delenv("AWS_READ_TIMEOUT", raising=False)
    aws.reset_for_testing()
    yield
    aws.reset_for_testing()


class TestDefaultConfig:
    def test_uses_the_documented_defaults(self):
        cfg = aws.default_config()
        assert cfg.connect_timeout == aws.DEFAULT_CONNECT_TIMEOUT
        assert cfg.read_timeout == aws.DEFAULT_READ_TIMEOUT
        assert cfg.retries["mode"] == "standard"
        assert cfg.retries["max_attempts"] == aws.DEFAULT_MAX_ATTEMPTS

    def test_connect_timeout_is_shorter_than_botocore_default(self):
        # The whole point: botocore waits 60s to establish a connection, which
        # turns a blackholed network into a minute of silence per attempt.
        assert aws.DEFAULT_CONNECT_TIMEOUT < 60

    def test_read_timeout_is_not_lowered(self):
        """Bedrock ad detection and Transcribe polling run close to 60s.

        Raising this is safe; lowering it would start timing out work that
        currently succeeds. This test exists to make that regression loud.
        """
        assert aws.DEFAULT_READ_TIMEOUT >= 60

    def test_retry_mode_is_not_adaptive(self):
        # adaptive adds a client-side token bucket that can block a caller for
        # an unbounded time, which is the wrong trade for a batch pipeline that
        # already retries at the application level.
        assert aws.DEFAULT_RETRY_MODE != "adaptive"

    @pytest.mark.parametrize(
        ("env", "attr", "value"),
        [
            ("AWS_CONNECT_TIMEOUT", "connect_timeout", 3.5),
            ("AWS_READ_TIMEOUT", "read_timeout", 180.0),
        ],
    )
    def test_timeouts_are_tunable_by_env(self, monkeypatch, env, attr, value):
        monkeypatch.setenv(env, str(value))
        assert getattr(aws.default_config(), attr) == value

    def test_unparseable_env_falls_back_with_a_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("AWS_CONNECT_TIMEOUT", "soon")
        with caplog.at_level(logging.WARNING, logger="aws"):
            cfg = aws.default_config()
        assert cfg.connect_timeout == aws.DEFAULT_CONNECT_TIMEOUT
        assert "is not a number" in caplog.text

    def test_empty_env_falls_back_silently(self, monkeypatch):
        monkeypatch.setenv("AWS_READ_TIMEOUT", "")
        assert aws.default_config().read_timeout == aws.DEFAULT_READ_TIMEOUT


class TestConfigure:
    def test_installs_a_default_session(self):
        assert boto3.DEFAULT_SESSION is None
        session = aws.configure()
        assert boto3.DEFAULT_SESSION is session

    def test_is_idempotent(self):
        assert aws.configure() is aws.configure()

    def test_force_rebuilds_the_session(self):
        first = aws.configure()
        assert aws.configure(force=True) is not first

    def test_recovers_if_the_default_session_is_cleared(self):
        aws.configure()
        boto3.DEFAULT_SESSION = None
        assert aws.configure() is not None
        assert boto3.DEFAULT_SESSION is not None

    def test_concurrent_calls_yield_one_session(self):
        """The RSS pipeline configures AWS while its worker threads build clients."""
        seen = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            seen.append(aws.configure())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(seen) == 8
        assert len({id(s) for s in seen}) == 1


class TestBareCallSitesInheritTheConfig:
    """The reason this module exists.

    19 call sites build clients with a bare ``boto3.client(...)``. Migrating
    them all (and the ~55 tests that patch them) would be a large mechanical
    diff, so instead the shared config is installed on boto3's default session.
    These tests prove that indirection actually reaches an untouched call site.
    """

    def test_bare_client_gets_the_shared_timeouts(self):
        aws.configure()
        c = boto3.client("s3", region_name="us-west-2")
        assert c.meta.config.connect_timeout == aws.DEFAULT_CONNECT_TIMEOUT
        assert c.meta.config.read_timeout == aws.DEFAULT_READ_TIMEOUT

    def test_bare_client_gets_the_shared_retry_mode(self):
        aws.configure()
        c = boto3.client("s3", region_name="us-west-2")
        assert c.meta.config.retries["mode"] == "standard"

    def test_env_override_reaches_a_bare_client(self, monkeypatch):
        monkeypatch.setenv("AWS_CONNECT_TIMEOUT", "2")
        aws.configure()
        assert boto3.client("s3", region_name="us-west-2").meta.config.connect_timeout == 2.0

    def test_all_services_share_one_session(self):
        aws.configure()
        clients = [
            boto3.client(svc, region_name="us-west-2")
            for svc in ("s3", "cloudfront", "transcribe", "bedrock-runtime")
        ]
        for c in clients:
            assert c.meta.config.connect_timeout == aws.DEFAULT_CONNECT_TIMEOUT

    def test_without_configure_botocore_defaults_apply(self):
        # Guards the test above from passing vacuously.
        c = boto3.client("s3", region_name="us-west-2")
        assert c.meta.config.connect_timeout == 60


class TestClientHelper:
    def test_configures_implicitly(self):
        assert boto3.DEFAULT_SESSION is None
        aws.client("s3", region="us-west-2")
        assert boto3.DEFAULT_SESSION is not None

    def test_applies_the_shared_config(self):
        c = aws.client("s3", region="us-west-2")
        assert c.meta.config.connect_timeout == aws.DEFAULT_CONNECT_TIMEOUT
        assert c.meta.config.retries["mode"] == "standard"

    def test_honours_the_region_argument(self):
        assert aws.client("s3", region="eu-west-1").meta.region_name == "eu-west-1"

    def test_extra_config_is_merged_over_the_defaults(self):
        c = aws.client("s3", region="us-west-2", config=Config(read_timeout=600))
        assert c.meta.config.read_timeout == 600
        # Unspecified values must survive the merge.
        assert c.meta.config.connect_timeout == aws.DEFAULT_CONNECT_TIMEOUT


class TestResetForTesting:
    def test_clears_the_shared_session(self):
        aws.configure()
        aws.reset_for_testing()
        assert boto3.DEFAULT_SESSION is None


class TestEntryPointsConfigureAws:
    """Bare call sites only inherit the config if some entry point installs it.

    That makes `configure()` easy to forget when a new entry point is added, so
    each one is pinned here.
    """

    def test_preflight_configures_before_checking_anything(self, monkeypatch):
        import preflight

        calls = []

        # Stub out every check so nothing touches the network. The assertion is
        # about ordering: AWS must be configured before the first client is built.
        for name in dir(preflight):
            if name.startswith("_check_"):
                monkeypatch.setattr(preflight, name, lambda *a, **k: calls.append("check"))
        monkeypatch.setattr(preflight.aws, "configure", lambda *a, **k: calls.append("aws"))

        preflight.run_preflight(dry_run=True)
        assert calls[0] == "aws", calls
        assert "check" in calls, calls

    def test_orchestrator_main_configures(self, monkeypatch):
        import orchestrator

        calls = []
        monkeypatch.setattr(aws, "configure", lambda *a, **k: calls.append("aws"))
        monkeypatch.setattr("logger_config.setup_logging", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "run_youtube_sources", lambda dry_run: True)

        assert orchestrator.main(["youtube"]) == 0
        assert calls == ["aws"]

    def test_mediasync_cli_configures(self, monkeypatch):
        from mediasync import cli

        calls = []
        monkeypatch.setattr(cli.aws, "configure", lambda *a, **k: calls.append("aws"))
        monkeypatch.setattr(
            cli.Config, "from_env", classmethod(lambda cls: (_ for _ in ()).throw(ValueError("x")))
        )

        assert cli.main([]) == 1
        assert calls == ["aws"]
