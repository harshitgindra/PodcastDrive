"""Shared pytest configuration.

This is the *single* place that puts the project source on ``sys.path``.
The root conftest is imported before any test module is collected, so every
test — including those under ``tests/mediasync/`` — inherits it. Individual
test modules must not re-insert the path.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent

# `src` holds both the flat top-level modules (ad_remover, sync, ...) and the
# `mediasync` package; `eval` holds the evaluation harness exercised by
# tests/test_run_eval.py.
for _path in (_ROOT / "src", _ROOT / "eval"):
    _entry = str(_path)
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

# ---------------------------------------------------------------------------
# Ensure boto3/botocore never tries to resolve a real AWS profile during tests.
# An empty AWS_PROFILE (common in developer shells) causes botocore to raise
# ProfileNotFound before moto's mock_aws() can intercept the call.
# ---------------------------------------------------------------------------
_AWS_TEST_ENV = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
}

# Strip AWS_PROFILE at import time so botocore never sees an empty profile name.
os.environ.pop("AWS_PROFILE", None)
# Set dummy credentials so boto3 can initialise without a real AWS config file.
for _k, _v in _AWS_TEST_ENV.items():
    os.environ.setdefault(_k, _v)


@pytest.fixture(autouse=True)
def _disable_retry_wait():
    """Patch tenacity wait times to zero so retry tests are fast."""
    with patch("time.sleep"):
        yield


@pytest.fixture(autouse=True)
def _reset_ffmpeg_crash_breaker():
    """The ffmpeg crash breaker is process-wide; keep it from leaking between tests.

    Some tests re-import ``ad_remover``, so the copy bound inside ``sync`` /
    ``podcast_sync`` can differ from ``sys.modules`` — reset every copy.
    """

    def _reset():
        copies = {id(m): m for m in (sys.modules.get("ad_remover"),) if m is not None}
        for holder in ("sync", "podcast_sync"):
            module = getattr(sys.modules.get(holder), "ad_remover", None)
            if module is not None:
                copies[id(module)] = module
        for module in copies.values():
            module._reset_ffmpeg_crash()

    _reset()
    yield
    _reset()
