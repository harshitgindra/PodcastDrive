"""Unit tests for utility functions."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from utils import extract_playlist_id, parse_upload_date, retry_aws_call

# ---------------------------------------------------------------------------
# retry_aws_call
# ---------------------------------------------------------------------------


def _make_client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "TestOp")


class TestRetryAwsCall:
    def test_returns_value_on_first_success(self):
        fn = MagicMock(return_value=42)
        assert retry_aws_call(fn, max_attempts=3, base_delay=0) == 42
        fn.assert_called_once()

    def test_retries_on_throttling_and_succeeds(self):
        fn = MagicMock(
            side_effect=[
                _make_client_error("Throttling"),
                _make_client_error("Throttling"),
                "ok",
            ]
        )
        with patch("time.sleep"):
            result = retry_aws_call(fn, max_attempts=5, base_delay=0)
        assert result == "ok"
        assert fn.call_count == 3

    def test_retries_on_service_unavailable(self):
        fn = MagicMock(
            side_effect=[
                _make_client_error("ServiceUnavailable"),
                "done",
            ]
        )
        with patch("time.sleep"):
            result = retry_aws_call(fn, max_attempts=3, base_delay=0)
        assert result == "done"
        assert fn.call_count == 2

    def test_raises_immediately_on_non_retryable_client_error(self):
        fn = MagicMock(side_effect=_make_client_error("AccessDenied"))
        with pytest.raises(ClientError) as exc_info:
            retry_aws_call(fn, max_attempts=5, base_delay=0)
        assert exc_info.value.response["Error"]["Code"] == "AccessDenied"
        fn.assert_called_once()  # no retry

    def test_raises_after_exhausting_all_attempts(self):
        fn = MagicMock(side_effect=_make_client_error("Throttling"))
        with patch("time.sleep"), pytest.raises(ClientError) as exc_info:
            retry_aws_call(fn, max_attempts=3, base_delay=0)
        assert exc_info.value.response["Error"]["Code"] == "Throttling"
        assert fn.call_count == 3

    def test_retries_on_connection_error(self):
        fn = MagicMock(side_effect=[ConnectionError("reset"), "value"])
        with patch("time.sleep"):
            result = retry_aws_call(fn, max_attempts=3, base_delay=0)
        assert result == "value"
        assert fn.call_count == 2

    def test_max_delay_cap_is_respected(self):
        """Sleep duration must never exceed max_delay."""
        sleep_calls = []
        fn = MagicMock(
            side_effect=[
                _make_client_error("Throttling"),
                _make_client_error("Throttling"),
                _make_client_error("Throttling"),
                "ok",
            ]
        )
        with patch("time.sleep", side_effect=lambda t: sleep_calls.append(t)):
            with patch("random.uniform", side_effect=lambda lo, hi: hi):
                retry_aws_call(fn, max_attempts=5, base_delay=1.0, max_delay=4.0)
        assert all(d <= 4.0 for d in sleep_calls), f"Sleep exceeded max_delay: {sleep_calls}"

    def test_label_used_in_log(self, caplog):
        import logging

        fn = MagicMock(side_effect=[_make_client_error("Throttling"), "ok"])
        with patch("time.sleep"), caplog.at_level(logging.WARNING, logger="utils"):
            retry_aws_call(fn, max_attempts=3, base_delay=0, label="my.operation")
        assert any("my.operation" in r.message for r in caplog.records)


# --- extract_playlist_id tests ---


class TestExtractPlaylistId:
    def test_standard_playlist_url(self):
        url = "https://www.youtube.com/playlist?list=PLEVkQGIATCXI1F2qs0slVE2MScaj1cSM0"
        assert extract_playlist_id(url) == "PLEVkQGIATCXI1F2qs0slVE2MScaj1cSM0"

    def test_url_with_extra_params(self):
        url = "https://www.youtube.com/playlist?list=PLabc123&index=5"
        assert extract_playlist_id(url) == "PLabc123"

    def test_watch_url_with_list_param(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz789"
        assert extract_playlist_id(url) == "PLxyz789"

    def test_missing_list_param_raises(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        with pytest.raises(ValueError, match="Could not extract playlist or channel ID"):
            extract_playlist_id(url)

    def test_empty_url_raises(self):
        with pytest.raises(ValueError, match="Could not extract playlist or channel ID"):
            extract_playlist_id("https://www.youtube.com")

    def test_no_query_string_raises(self):
        url = "https://www.youtube.com/playlist"
        with pytest.raises(ValueError, match="Could not extract playlist or channel ID"):
            extract_playlist_id(url)

    def test_channel_handle_url(self):
        url = "https://www.youtube.com/@MyChannel/videos"
        assert extract_playlist_id(url) == "@MyChannel"

    def test_channel_handle_url_without_videos(self):
        url = "https://www.youtube.com/@SomeHandle"
        assert extract_playlist_id(url) == "@SomeHandle"

    def test_channel_id_url(self):
        url = "https://www.youtube.com/channel/UCabcdef1234567890"
        assert extract_playlist_id(url) == "UCabcdef1234567890"

    def test_raw_playlist_id_returned_as_is(self):
        assert extract_playlist_id("PLEVkQGIATCXI1F2qs0") == "PLEVkQGIATCXI1F2qs0"

    def test_raw_channel_handle_returned_as_is(self):
        assert extract_playlist_id("@MyChannel") == "@MyChannel"

    def test_raw_uc_id_returned_as_is(self):
        assert extract_playlist_id("UCabcdef1234567890") == "UCabcdef1234567890"


# --- parse_upload_date tests ---


class TestParseUploadDate:
    def test_valid_date(self):
        result = parse_upload_date("20250115")
        assert result == datetime(2025, 1, 15, tzinfo=UTC)

    def test_result_has_utc_timezone(self):
        result = parse_upload_date("20240601")
        assert result.tzinfo == UTC

    def test_invalid_string_falls_back_to_epoch(self):
        result = parse_upload_date("not-a-date")
        assert result == datetime(1970, 1, 1, tzinfo=UTC)

    def test_empty_string_falls_back_to_epoch(self):
        result = parse_upload_date("")
        assert result == datetime(1970, 1, 1, tzinfo=UTC)

    def test_partial_date_falls_back_to_epoch(self):
        result = parse_upload_date("202501")
        assert result == datetime(1970, 1, 1, tzinfo=UTC)

    def test_wrong_format_falls_back_to_epoch(self):
        result = parse_upload_date("2025-01-15")
        assert result == datetime(1970, 1, 1, tzinfo=UTC)

    def test_leap_year_date(self):
        result = parse_upload_date("20240229")
        assert result == datetime(2024, 2, 29, tzinfo=UTC)

    def test_invalid_day_falls_back_to_epoch(self):
        result = parse_upload_date("20250230")  # Feb 30 doesn't exist
        assert result == datetime(1970, 1, 1, tzinfo=UTC)


# ── _validate_playlist_id edge cases ─────────────────────────────────────────


class TestValidatePlaylistId:
    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="empty"):
            extract_playlist_id("")

    def test_unsafe_characters_raise(self):
        with pytest.raises(ValueError, match="unsafe characters"):
            extract_playlist_id("PL../../../etc/passwd")

    def test_path_traversal_raises(self):
        with pytest.raises(ValueError, match="path traversal"):
            extract_playlist_id("PL..secret")
