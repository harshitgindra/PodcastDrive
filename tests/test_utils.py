"""Unit tests for utility functions."""

from datetime import datetime, timezone

import pytest

from utils import extract_playlist_id, parse_upload_date


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


# --- parse_upload_date tests ---


class TestParseUploadDate:
    def test_valid_date(self):
        result = parse_upload_date("20250115")
        assert result == datetime(2025, 1, 15, tzinfo=timezone.utc)

    def test_result_has_utc_timezone(self):
        result = parse_upload_date("20240601")
        assert result.tzinfo == timezone.utc

    def test_invalid_string_falls_back_to_today(self):
        result = parse_upload_date("not-a-date")
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        assert result == today

    def test_empty_string_falls_back_to_today(self):
        result = parse_upload_date("")
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        assert result == today

    def test_partial_date_falls_back_to_today(self):
        result = parse_upload_date("202501")
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        assert result == today

    def test_wrong_format_falls_back_to_today(self):
        result = parse_upload_date("2025-01-15")
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        assert result == today

    def test_leap_year_date(self):
        result = parse_upload_date("20240229")
        assert result == datetime(2024, 2, 29, tzinfo=timezone.utc)

    def test_invalid_day_falls_back_to_today(self):
        result = parse_upload_date("20250230")  # Feb 30 doesn't exist
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        assert result == today
