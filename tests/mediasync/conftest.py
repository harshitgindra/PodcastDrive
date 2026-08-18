"""MediaSync-specific test fixtures.

Path setup lives in the root ``tests/conftest.py``.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_env_file_writes(tmp_path, monkeypatch):
    """Prevent tests from writing to the real mediasync.env.

    OneDriveClient._persist_rotated_token uses Path.cwd() to find the env file.
    Without isolation, any test that instantiates OneDriveClient will overwrite
    the real mediasync.env with mock token values.
    """
    monkeypatch.chdir(tmp_path)
