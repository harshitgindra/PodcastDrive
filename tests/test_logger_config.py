"""Unit tests for logger_config module."""

import logging
import os
import tempfile
from unittest.mock import patch

from logger_config import setup_logging


def _remove_non_pytest_handlers():
    """Remove only non-pytest handlers so setup_logging() can add fresh ones."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        # pytest installs LogCaptureHandler — leave those alone
        if type(handler).__name__ not in ("LogCaptureHandler",):
            handler.close()
            root.removeHandler(handler)


class TestSetupLogging:
    def setup_method(self):
        _remove_non_pytest_handlers()

    def teardown_method(self):
        _remove_non_pytest_handlers()

    def test_adds_console_handler(self):
        """Verify setup_logging adds a StreamHandler to the root logger."""
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers = []
        try:
            with tempfile.TemporaryDirectory() as log_dir:
                with patch("logger_config._IS_LAMBDA", True):  # skip file handler
                    setup_logging(log_dir=log_dir)
                    handler_types = [type(h).__name__ for h in root.handlers]
                    assert "StreamHandler" in handler_types
        finally:
            for h in root.handlers[:]:
                if type(h).__name__ not in ("LogCaptureHandler",):
                    h.close()
                    root.removeHandler(h)
            root.handlers.extend(original_handlers)

    def test_adds_file_handler_locally(self):
        """Verify setup_logging creates a TimedRotatingFileHandler when not Lambda."""
        with tempfile.TemporaryDirectory() as log_dir, patch("logger_config._IS_LAMBDA", False):
            _remove_non_pytest_handlers()
            # Temporarily replace root logger handlers list
            root = logging.getLogger()
            original_handlers = root.handlers[:]
            root.handlers = []
            try:
                setup_logging(log_dir=log_dir)
                handler_types = [type(h).__name__ for h in root.handlers]
                assert "TimedRotatingFileHandler" in handler_types
            finally:
                for h in root.handlers[:]:
                    if type(h).__name__ not in ("LogCaptureHandler",):
                        h.close()
                        root.removeHandler(h)
                root.handlers.extend(original_handlers)

    def test_no_file_handler_on_lambda(self):
        """Verify no file handler is added when running on Lambda."""
        with tempfile.TemporaryDirectory() as log_dir, patch("logger_config._IS_LAMBDA", True):
            root = logging.getLogger()
            original_handlers = root.handlers[:]
            root.handlers = []
            try:
                setup_logging(log_dir=log_dir)
                handler_types = [type(h).__name__ for h in root.handlers]
                assert "TimedRotatingFileHandler" not in handler_types
            finally:
                for h in root.handlers[:]:
                    if type(h).__name__ not in ("LogCaptureHandler",):
                        h.close()
                        root.removeHandler(h)
                root.handlers.extend(original_handlers)

    def test_log_level_applied(self):
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers = []
        original_level = root.level
        try:
            with tempfile.TemporaryDirectory() as log_dir:
                setup_logging(log_dir=log_dir, log_level="DEBUG")
                assert root.level == logging.DEBUG
        finally:
            for h in root.handlers[:]:
                if type(h).__name__ not in ("LogCaptureHandler",):
                    h.close()
                    root.removeHandler(h)
            root.handlers.extend(original_handlers)
            root.setLevel(original_level)

    def test_log_level_from_env_var(self):
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        original_level = root.level
        root.handlers = []
        try:
            with tempfile.TemporaryDirectory() as log_dir, patch.dict(os.environ, {"LOG_LEVEL": "WARNING"}):
                setup_logging(log_dir=log_dir)
                assert root.level == logging.WARNING
        finally:
            for h in root.handlers[:]:
                if type(h).__name__ not in ("LogCaptureHandler",):
                    h.close()
                    root.removeHandler(h)
            root.handlers.extend(original_handlers)
            root.setLevel(original_level)

    def test_log_dir_created_if_missing(self):
        with tempfile.TemporaryDirectory() as base:
            log_dir = os.path.join(base, "new_subdir", "logs")
            with patch("logger_config._IS_LAMBDA", False):
                root = logging.getLogger()
                original_handlers = root.handlers[:]
                root.handlers = []
                try:
                    setup_logging(log_dir=log_dir)
                    assert os.path.isdir(log_dir)
                finally:
                    for h in root.handlers[:]:
                        if type(h).__name__ not in ("LogCaptureHandler",):
                            h.close()
                            root.removeHandler(h)
                    root.handlers.extend(original_handlers)

    def test_log_file_created(self):
        with tempfile.TemporaryDirectory() as log_dir, patch("logger_config._IS_LAMBDA", False):
            root = logging.getLogger()
            original_handlers = root.handlers[:]
            root.handlers = []
            try:
                setup_logging(log_dir=log_dir)
                log_file = os.path.join(log_dir, "playlist_downloader.log")
                assert os.path.exists(log_file)
            finally:
                for h in root.handlers[:]:
                    if type(h).__name__ not in ("LogCaptureHandler",):
                        h.close()
                        root.removeHandler(h)
                root.handlers.extend(original_handlers)

    def test_idempotent_second_call_does_not_add_duplicate_handlers(self):
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers = []
        try:
            with tempfile.TemporaryDirectory() as log_dir:
                setup_logging(log_dir=log_dir)
                count_before = len(root.handlers)
                setup_logging(log_dir=log_dir)
                count_after = len(root.handlers)
                assert count_before == count_after
        finally:
            for h in root.handlers[:]:
                if type(h).__name__ not in ("LogCaptureHandler",):
                    h.close()
                    root.removeHandler(h)
            root.handlers.extend(original_handlers)

    def test_retention_days_from_env_var(self):
        with tempfile.TemporaryDirectory() as log_dir, patch("logger_config._IS_LAMBDA", False):
            root = logging.getLogger()
            original_handlers = root.handlers[:]
            root.handlers = []
            try:
                with patch.dict(os.environ, {"LOG_RETENTION_DAYS": "5"}):
                    setup_logging(log_dir=log_dir)
                    file_handlers = [h for h in root.handlers if type(h).__name__ == "TimedRotatingFileHandler"]
                    assert len(file_handlers) == 1
                    assert file_handlers[0].backupCount == 5
            finally:
                for h in root.handlers[:]:
                    if type(h).__name__ not in ("LogCaptureHandler",):
                        h.close()
                        root.removeHandler(h)
                root.handlers.extend(original_handlers)

    def test_log_dir_from_env_var(self):
        with tempfile.TemporaryDirectory() as base:
            log_dir = os.path.join(base, "env_logs")
            with patch("logger_config._IS_LAMBDA", False):
                root = logging.getLogger()
                original_handlers = root.handlers[:]
                root.handlers = []
                try:
                    with patch.dict(os.environ, {"LOG_DIR": log_dir}):
                        setup_logging()
                        assert os.path.isdir(log_dir)
                finally:
                    for h in root.handlers[:]:
                        if type(h).__name__ not in ("LogCaptureHandler",):
                            h.close()
                            root.removeHandler(h)
                    root.handlers.extend(original_handlers)


# ---------------------------------------------------------------------------
# _JsonFormatter
# ---------------------------------------------------------------------------


class TestJsonFormatter:
    def _get_formatter(self):
        from logger_config import _JsonFormatter

        return _JsonFormatter()

    def test_format_returns_valid_json(self):
        import json

        formatter = self._get_formatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        doc = json.loads(output)
        assert doc["message"] == "hello world"
        assert doc["level"] == "INFO"
        assert doc["logger"] == "test.logger"
        assert "timestamp" in doc

    def test_format_includes_exc_info(self):
        import json

        formatter = self._get_formatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc = sys.exc_info()

        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="error occurred",
            args=(),
            exc_info=exc,
        )
        output = formatter.format(record)
        doc = json.loads(output)
        assert "exc_info" in doc
        assert "ValueError" in doc["exc_info"]

    def test_format_merges_extra_fields(self):
        import json

        formatter = self._get_formatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="msg with extra",
            args=(),
            exc_info=None,
        )
        record.playlist_id = "PLtest"
        output = formatter.format(record)
        doc = json.loads(output)
        assert doc.get("playlist_id") == "PLtest"
