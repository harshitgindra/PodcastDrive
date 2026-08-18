"""Tests for mediasync.retry module."""

from unittest.mock import MagicMock, patch

import pytest

from mediasync.downloader import DownloadError
from mediasync.retry import (
    is_transient_download_error,
    retry_on_error,
)


class TestRetryOnError:
    def test_succeeds_first_try(self):
        fn = MagicMock(return_value="ok")
        result = retry_on_error(fn, description="test")
        assert result == "ok"
        assert fn.call_count == 1

    def test_succeeds_after_retries(self):
        fn = MagicMock(side_effect=[ValueError("fail"), ValueError("fail"), "ok"])
        with patch("mediasync.retry.time.sleep"):
            result = retry_on_error(fn, max_retries=3, description="test")
        assert result == "ok"
        assert fn.call_count == 3

    def test_raises_after_exhausting_retries(self):
        fn = MagicMock(side_effect=ValueError("always fails"))
        with patch("mediasync.retry.time.sleep"):
            with pytest.raises(ValueError, match="always fails"):
                retry_on_error(fn, max_retries=2, description="test")
        assert fn.call_count == 3  # initial + 2 retries

    def test_no_retry_when_max_retries_zero(self):
        fn = MagicMock(side_effect=ValueError("fail"))
        with pytest.raises(ValueError):
            retry_on_error(fn, max_retries=0, description="test")
        assert fn.call_count == 1

    def test_respects_retryable_predicate(self):
        fn = MagicMock(side_effect=ValueError("permanent"))
        with pytest.raises(ValueError):
            retry_on_error(
                fn,
                max_retries=3,
                retryable=lambda exc: False,
                description="test",
            )
        assert fn.call_count == 1

    def test_exponential_backoff_delays(self):
        fn = MagicMock(side_effect=[ValueError("1"), ValueError("2"), "ok"])
        with patch("mediasync.retry.time.sleep") as mock_sleep:
            retry_on_error(fn, max_retries=3, base_delay=1.0, description="test")
        # First retry: 1.0 * 2^0 = 1.0, second: 1.0 * 2^1 = 2.0
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == 1.0
        assert mock_sleep.call_args_list[1][0][0] == 2.0

    def test_delay_capped_at_max(self):
        fn = MagicMock(side_effect=[ValueError("1"), ValueError("2"), ValueError("3"), "ok"])
        with patch("mediasync.retry.time.sleep") as mock_sleep:
            retry_on_error(fn, max_retries=3, base_delay=10.0, max_delay=15.0, description="test")
        # Delays: min(10*2^0,15)=10, min(10*2^1,15)=15, min(10*2^2,15)=15
        assert mock_sleep.call_args_list[0][0][0] == 10.0
        assert mock_sleep.call_args_list[1][0][0] == 15.0
        assert mock_sleep.call_args_list[2][0][0] == 15.0


class TestIsTransientDownloadError:
    @pytest.mark.parametrize("msg", [
        "HTTP Error 429: Too Many Requests",
        "HTTP Error 503: Service Unavailable",
        "HTTP Error 502: Bad Gateway",
        "HTTP Error 500: Internal Server Error",
        "Connection reset by peer",
        "Connection refused",
        "Read timed out",
        "Network is unreachable",
        "SSL: CERTIFICATE_VERIFY_FAILED",
        "EOF occurred in violation of protocol",
        "IncompleteRead: 0 bytes read",
        "Broken pipe",
    ])
    def test_transient_errors_return_true(self, msg):
        assert is_transient_download_error(DownloadError(msg)) is True

    @pytest.mark.parametrize("msg", [
        "Video unavailable",
        "Private video",
        "This video has been removed",
        "Age-restricted content",
        "Geo-restricted",
        "This content is not available in your country",
        "Copyright claim",
        "Account terminated",
        "Sign in to confirm your age",
        "Members-only content",
    ])
    def test_permanent_errors_return_false(self, msg):
        assert is_transient_download_error(DownloadError(msg)) is False

    def test_unknown_error_defaults_to_transient(self):
        assert is_transient_download_error(DownloadError("something unexpected")) is True
