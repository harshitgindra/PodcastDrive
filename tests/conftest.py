"""Configure pytest to find src/ modules."""

import os
import sys
from unittest.mock import patch

import pytest

# Add src/ to the path so test files can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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
