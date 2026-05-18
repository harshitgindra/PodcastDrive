"""Configure pytest to find src/ modules."""
import sys
import os
from unittest.mock import patch

import pytest

# Add src/ to the path so test files can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture(autouse=True)
def _disable_retry_wait():
    """Patch tenacity wait times to zero so retry tests are fast."""
    with patch("time.sleep"):
        yield
