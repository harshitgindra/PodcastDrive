"""Ensure src/ is on path for mediasync tests."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))


@pytest.fixture(autouse=True)
def _isolate_env_file_writes(tmp_path, monkeypatch):
    """Prevent tests from writing to the real mediasync.env.

    OneDriveClient._persist_rotated_token uses Path.cwd() to find the env file.
    Without isolation, any test that instantiates OneDriveClient will overwrite
    the real mediasync.env with mock token values.
    """
    monkeypatch.chdir(tmp_path)
