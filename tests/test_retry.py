"""Tests for the shared retry engine and its transient/permanent predicates."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointResolutionError

from retry import (
    RETRYABLE_AWS_CODES,
    is_transient_aws_error,
    is_transient_download_error,
    retry_call,
)


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "TestOp")


class TestRetryCallAttempts:
    def test_returns_first_success_without_sleeping(self):
        fn = MagicMock(return_value="ok")
        with patch("retry.time.sleep") as sleep:
            assert retry_call(fn) == "ok"
        fn.assert_called_once()
        sleep.assert_not_called()

    def test_attempts_one_disables_retrying(self):
        fn = MagicMock(side_effect=RuntimeError("boom"))
        with patch("retry.time.sleep") as sleep, pytest.raises(RuntimeError):
            retry_call(fn, attempts=1)
        fn.assert_called_once()
        sleep.assert_not_called()

    def test_retries_until_success(self):
        fn = MagicMock(side_effect=[RuntimeError("a"), RuntimeError("b"), "ok"])
        with patch("retry.time.sleep"):
            assert retry_call(fn, attempts=5, base_delay=0) == "ok"
        assert fn.call_count == 3

    def test_exhausting_attempts_reraises_the_last_exception(self):
        fn = MagicMock(side_effect=[RuntimeError("first"), RuntimeError("last")])
        with patch("retry.time.sleep"), pytest.raises(RuntimeError, match="last"):
            retry_call(fn, attempts=2, base_delay=0)
        assert fn.call_count == 2

    def test_does_not_sleep_before_giving_up(self):
        """A back-off it can never use is pure waste, so the last failure raises at once."""
        fn = MagicMock(side_effect=RuntimeError("boom"))
        with patch("retry.time.sleep") as sleep, pytest.raises(RuntimeError):
            retry_call(fn, attempts=3, base_delay=1.0)
        assert fn.call_count == 3
        assert sleep.call_count == 2  # not 3


class TestRetryCallBackoff:
    def test_delays_double(self):
        fn = MagicMock(side_effect=[RuntimeError()] * 3 + ["ok"])
        with patch("retry.time.sleep") as sleep:
            retry_call(fn, attempts=5, base_delay=2.0, max_delay=1000)
        assert [c[0][0] for c in sleep.call_args_list] == [2.0, 4.0, 8.0]

    def test_delays_are_capped(self):
        fn = MagicMock(side_effect=[RuntimeError()] * 4 + ["ok"])
        with patch("retry.time.sleep") as sleep:
            retry_call(fn, attempts=6, base_delay=10.0, max_delay=15.0)
        assert [c[0][0] for c in sleep.call_args_list] == [10.0, 15.0, 15.0, 15.0]

    def test_no_jitter_by_default(self):
        fn = MagicMock(side_effect=[RuntimeError(), "ok"])
        with patch("retry.time.sleep") as sleep, patch("random.uniform") as uniform:
            retry_call(fn, attempts=3, base_delay=4.0)
        uniform.assert_not_called()
        assert sleep.call_args_list[0][0][0] == 4.0

    def test_jitter_adds_up_to_half_the_delay(self):
        fn = MagicMock(side_effect=[RuntimeError(), "ok"])
        with (
            patch("retry.time.sleep") as sleep,
            patch("random.uniform", side_effect=lambda lo, hi: hi) as uniform,
        ):
            retry_call(fn, attempts=3, base_delay=4.0, max_delay=100, jitter=True)
        uniform.assert_called_once_with(0, 2.0)
        assert sleep.call_args_list[0][0][0] == 6.0

    def test_jitter_never_exceeds_max_delay(self):
        fn = MagicMock(side_effect=[RuntimeError()] * 4 + ["ok"])
        with (
            patch("retry.time.sleep") as sleep,
            patch("random.uniform", side_effect=lambda lo, hi: hi),
        ):
            retry_call(fn, attempts=6, base_delay=1.0, max_delay=4.0, jitter=True)
        assert all(d <= 4.0 for d in (c[0][0] for c in sleep.call_args_list))


class TestRetryCallPredicate:
    def test_non_retryable_raises_immediately(self):
        fn = MagicMock(side_effect=ValueError("permanent"))
        with patch("retry.time.sleep") as sleep, pytest.raises(ValueError):
            retry_call(fn, attempts=5, retryable=lambda exc: False)
        fn.assert_called_once()
        sleep.assert_not_called()

    def test_predicate_can_select_by_type(self):
        fn = MagicMock(side_effect=[OSError("transient"), ValueError("permanent")])
        with patch("retry.time.sleep"), pytest.raises(ValueError):
            retry_call(fn, attempts=5, base_delay=0, retryable=lambda e: isinstance(e, OSError))
        assert fn.call_count == 2

    def test_default_predicate_retries_everything(self):
        fn = MagicMock(side_effect=[ValueError(), "ok"])
        with patch("retry.time.sleep"):
            assert retry_call(fn, attempts=2, base_delay=0) == "ok"


class TestRetryCallLogging:
    def test_uses_supplied_logger(self, caplog):
        fn = MagicMock(side_effect=[RuntimeError("x"), "ok"])
        with patch("retry.time.sleep"), caplog.at_level(logging.WARNING, logger="my.caller"):
            retry_call(fn, attempts=2, base_delay=0, logger=logging.getLogger("my.caller"))
        assert [r.name for r in caplog.records] == ["my.caller"]

    def test_label_appears_in_the_warning(self, caplog):
        fn = MagicMock(side_effect=[RuntimeError("x"), "ok"])
        with patch("retry.time.sleep"), caplog.at_level(logging.WARNING, logger="retry"):
            retry_call(fn, attempts=2, base_delay=0, label="s3.put_object")
        assert "s3.put_object" in caplog.text

    def test_falls_back_to_the_callable_name(self, caplog):
        def named_operation():
            raise RuntimeError("x")

        with (
            patch("retry.time.sleep"),
            caplog.at_level(logging.WARNING, logger="retry"),
            pytest.raises(RuntimeError),
        ):
            retry_call(named_operation, attempts=2, base_delay=0)
        assert "named_operation" in caplog.text


class TestIsTransientAwsError:
    @pytest.mark.parametrize("code", sorted(RETRYABLE_AWS_CODES))
    def test_every_listed_code_is_transient(self, code):
        assert is_transient_aws_error(client_error(code)) is True

    @pytest.mark.parametrize(
        "code",
        ["AccessDenied", "NoSuchBucket", "NoSuchKey", "ValidationException", "InvalidParameter"],
    )
    def test_permanent_codes_are_not_transient(self, code):
        assert is_transient_aws_error(client_error(code)) is False

    def test_missing_error_code_is_not_transient(self):
        assert is_transient_aws_error(ClientError({}, "TestOp")) is False

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionError("reset"),
            OSError("broken"),
            TimeoutError("timed out"),  # subclass of OSError
            EndpointResolutionError(msg="could not resolve endpoint"),
        ],
    )
    def test_transport_failures_are_transient(self, exc):
        assert is_transient_aws_error(exc) is True

    @pytest.mark.parametrize("exc", [ValueError("nope"), KeyError("nope"), RuntimeError("nope")])
    def test_unrelated_exceptions_are_not_transient(self, exc):
        assert is_transient_aws_error(exc) is False

    def test_codes_the_old_utils_set_had_are_preserved(self):
        # Guard against silently dropping codes while merging the two sets.
        for code in ("RequestExpired", "EC2ThrottledException", "SlowDown", "InternalFailure"):
            assert code in RETRYABLE_AWS_CODES


class TestIsTransientDownloadError:
    @pytest.mark.parametrize(
        "message",
        [
            "HTTP Error 429: Too Many Requests",
            "HTTP Error 503: Service Unavailable",
            "HTTP Error 502: Bad Gateway",
            "HTTP Error 500: Internal Server Error",
            "Connection reset by peer",
            "Connection refused",
            "The read operation timed out",
            "urlopen error timeout",
            "Temporary failure in name resolution",
            "network is unreachable",
            "SSL: WRONG_VERSION_NUMBER",
            "EOF occurred in violation of protocol",
            "IncompleteRead(1234 bytes read)",
            "Broken pipe",
        ],
    )
    def test_transient_messages(self, message):
        assert is_transient_download_error(Exception(message)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "ERROR: [youtube] abc: Video unavailable",
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
            "This video has been removed by the uploader",
            "Sign in to confirm your age. This video may be age-restricted",
            "The uploader has not made this video available in your country",  # geo
            "Video unavailable. This video contains content from X who has blocked it on copyright grounds",
            "This channel has been terminated",
            "Join this channel to get access to members-only content",
            "The video is DRM protected",
        ],
    )
    def test_permanent_messages(self, message):
        assert is_transient_download_error(Exception(message)) is False

    def test_unknown_errors_are_assumed_transient(self):
        # A needless retry costs seconds; wrongly giving up loses an episode.
        assert is_transient_download_error(Exception("something odd happened")) is True

    def test_transient_marker_wins_over_permanent_marker(self):
        # A 503 while the message also mentions "not available" is still worth retrying.
        assert is_transient_download_error(Exception("HTTP 503, format not available")) is True

    def test_classification_is_case_insensitive(self):
        assert is_transient_download_error(Exception("VIDEO UNAVAILABLE")) is False
        assert is_transient_download_error(Exception("CONNECTION RESET")) is True
