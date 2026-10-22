"""Tests for mediasync.notion_client module."""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from mediasync.notion_client import (
    MIN_REQUEST_INTERVAL_SECS,
    REQUEST_ATTEMPTS,
    Format,
    NotionClient,
    NotionError,
    Status,
)


@pytest.fixture
def client():
    """NotionClient with test credentials."""
    return NotionClient("test-token", "test-db-id")


@pytest.fixture
def sample_page():
    """A complete Notion page dict."""
    return {
        "id": "page-123",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "https://youtube.com/watch?v=abc"}]},
            "Profile": {"type": "select", "select": {"name": "harshit"}},
            "Format": {"type": "select", "select": {"name": "audio"}},
            "Status": {"type": "select", "select": {"name": "pending"}},
            "Delete": {"type": "checkbox", "checkbox": False},
            "File Key": {"type": "rich_text", "rich_text": []},
        },
    }


@pytest.fixture
def done_page():
    """A page with status=done and a file key."""
    return {
        "id": "page-456",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "https://youtube.com/watch?v=xyz"}]},
            "Profile": {"type": "select", "select": {"name": "harshit"}},
            "Format": {"type": "select", "select": {"name": "video"}},
            "Status": {"type": "select", "select": {"name": "done"}},
            "Delete": {"type": "checkbox", "checkbox": False},
            "File Key": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "/MediaSync/harshit/video/test.mp4"}],
            },
        },
    }


class TestParsePage:
    def test_parses_title_url(self, client, sample_page):
        entry = client._parse_page(sample_page)
        assert entry is not None
        assert entry.url == "https://youtube.com/watch?v=abc"
        assert entry.profile == "harshit"
        assert entry.format == Format.AUDIO
        assert entry.status == Status.PENDING
        assert entry.delete is False
        assert entry.file_key == ""

    def test_strips_whitespace_from_url(self, client, sample_page):
        sample_page["properties"]["Name"]["title"][0]["plain_text"] = "  https://youtube.com/watch?v=rt1  "
        entry = client._parse_page(sample_page)
        assert entry.url == "https://youtube.com/watch?v=rt1"

    def test_parses_file_key(self, client, done_page):
        entry = client._parse_page(done_page)
        assert entry.file_key == "/MediaSync/harshit/video/test.mp4"

    def test_returns_none_for_empty_url(self, client, sample_page):
        sample_page["properties"]["Name"] = {"type": "title", "title": []}
        assert client._parse_page(sample_page) is None

    def test_returns_none_for_missing_profile(self, client, sample_page):
        del sample_page["properties"]["Profile"]
        assert client._parse_page(sample_page) is None

    def test_returns_none_for_invalid_format(self, client, sample_page):
        sample_page["properties"]["Format"]["select"]["name"] = "INVALID"
        assert client._parse_page(sample_page) is None

    def test_null_status_treated_as_pending(self, client, sample_page):
        sample_page["properties"]["Status"] = {"type": "select", "select": None}
        entry = client._parse_page(sample_page)
        assert entry is not None
        assert entry.status == Status.PENDING

    def test_empty_status_name_treated_as_pending(self, client, sample_page):
        sample_page["properties"]["Status"] = {"type": "select", "select": {"name": ""}}
        entry = client._parse_page(sample_page)
        assert entry.status == Status.PENDING

    def test_parses_both_format(self, client, sample_page):
        sample_page["properties"]["Format"]["select"]["name"] = "both"
        entry = client._parse_page(sample_page)
        assert entry.format == Format.BOTH

    def test_case_insensitive_format(self, client, sample_page):
        sample_page["properties"]["Format"]["select"]["name"] = "Audio"
        entry = client._parse_page(sample_page)
        assert entry.format == Format.AUDIO


class TestQuery:
    def test_get_pending(self, client, sample_page):
        response = {
            "results": [sample_page],
            "has_more": False,
            "next_cursor": None,
        }
        with patch.object(client, "_post", return_value=response) as mock_post:
            entries = client.get_pending()

        assert len(entries) == 1
        assert entries[0].status == Status.PENDING
        # Verify filter includes or clause for empty/pending status
        call_args = mock_post.call_args[0]
        payload = call_args[1]
        status_filter = payload["filter"]["and"][0]
        assert "or" in status_filter
        assert {"property": "Status", "select": {"equals": "pending"}} in status_filter["or"]
        assert {"property": "Status", "select": {"is_empty": True}} in status_filter["or"]

    def test_get_deletions(self, client, done_page):
        done_page["properties"]["Delete"]["checkbox"] = True
        response = {"results": [done_page], "has_more": False, "next_cursor": None}
        with patch.object(client, "_post", return_value=response):
            entries = client.get_deletions()

        assert len(entries) == 1
        assert entries[0].delete is True
        assert entries[0].status == Status.DONE

    def test_get_done_for_profile(self, client, done_page):
        response = {"results": [done_page], "has_more": False, "next_cursor": None}
        with patch.object(client, "_post", return_value=response) as mock_post:
            entries = client.get_done_for_profile("harshit")

        assert len(entries) == 1
        payload = mock_post.call_args[0][1]
        filters = payload["filter"]["and"]
        assert {"property": "Profile", "select": {"equals": "harshit"}} in filters

    def test_pagination(self, client, sample_page):
        page2 = dict(sample_page)
        page2["id"] = "page-789"

        response1 = {"results": [sample_page], "has_more": True, "next_cursor": "cursor-1"}
        response2 = {"results": [page2], "has_more": False, "next_cursor": None}

        with patch.object(client, "_post", side_effect=[response1, response2]) as mock_post:
            entries = client.get_pending()

        assert len(entries) == 2
        # Second call should include start_cursor
        second_payload = mock_post.call_args_list[1][0][1]
        assert second_payload["start_cursor"] == "cursor-1"

    def test_api_failure_raises_instead_of_looking_empty(self, client):
        with patch.object(client, "_post", return_value=None):
            with pytest.raises(NotionError, match="Failed to query pending"):
                client.get_pending()


class TestUpdateStatus:
    def test_update_to_downloading(self, client):
        with patch.object(client, "_patch", return_value={"id": "page-123"}) as mock_patch:
            client.update_status("page-123", Status.DOWNLOADING)

        call_args = mock_patch.call_args[0]
        payload = call_args[1]
        assert payload["properties"]["Status"] == {"select": {"name": "downloading"}}
        assert "Processed At" not in payload["properties"]

    def test_update_to_done_includes_timestamp(self, client):
        with patch.object(client, "_patch", return_value={"id": "page-123"}) as mock_patch:
            client.update_status("page-123", Status.DONE, file_key="/MediaSync/h/audio/t.m4a", duration=180)

        payload = mock_patch.call_args[0][1]
        props = payload["properties"]
        assert props["Status"] == {"select": {"name": "done"}}
        assert props["File Key"]["rich_text"][0]["text"]["content"] == "/MediaSync/h/audio/t.m4a"
        assert props["Duration"] == {"number": 180}
        assert "Processed At" in props

    def test_update_to_failed_with_error(self, client):
        with patch.object(client, "_patch", return_value={"id": "page-123"}) as mock_patch:
            client.update_status("page-123", Status.FAILED, error="Network timeout")

        payload = mock_patch.call_args[0][1]
        props = payload["properties"]
        assert props["Error"]["rich_text"][0]["text"]["content"] == "Network timeout"

    def test_error_truncated_to_2000_chars(self, client):
        long_error = "x" * 5000
        with patch.object(client, "_patch", return_value={"id": "page-123"}) as mock_patch:
            client.update_status("page-123", Status.FAILED, error=long_error)

        payload = mock_patch.call_args[0][1]
        error_text = payload["properties"]["Error"]["rich_text"][0]["text"]["content"]
        assert len(error_text) == 2000


class TestArchivePage:
    def test_archives_page(self, client):
        with patch.object(client, "_patch", return_value={"id": "page-123"}) as mock_patch:
            client.archive_page("page-123")

        call_args = mock_patch.call_args[0]
        assert call_args[1] == {"archived": True}
        assert "page-123" in call_args[0]

    def test_archive_failure_raises(self, client):
        with patch.object(client, "_patch", return_value=None):
            with pytest.raises(NotionError, match="Failed to archive"):
                client.archive_page("page-123")


class TestHttpMethods:
    """Test _post and _patch with mocked urlopen."""

    def test_post_success(self, client):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"results": [], "has_more": False}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._post("https://api.notion.com/v1/test", {"key": "value"})

        assert result == {"results": [], "has_more": False}

    def test_post_failure_returns_none(self, client):
        with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
            result = client._post("https://api.notion.com/v1/test", {})

        assert result is None

    def test_patch_success(self, client):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": "page-1"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._patch("https://api.notion.com/v1/pages/p1", {"archived": True})

        assert result == {"id": "page-1"}

    def test_patch_failure_returns_none(self, client):
        with patch("urllib.request.urlopen", side_effect=OSError("Timeout")):
            result = client._patch("https://api.notion.com/v1/pages/p1", {})

        assert result is None


class TestParsePageEdgeCases:
    def test_missing_name_field_returns_none(self, client):
        page = {
            "id": "p1",
            "properties": {
                "Profile": {"type": "select", "select": {"name": "h"}},
                "Format": {"type": "select", "select": {"name": "audio"}},
                "Status": {"type": "select", "select": {"name": "pending"}},
                "Delete": {"type": "checkbox", "checkbox": False},
                "File Key": {"type": "rich_text", "rich_text": []},
            },
        }
        assert client._parse_page(page) is None

    def test_whitespace_only_url_returns_none(self, client):
        page = {
            "id": "p1",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "   "}]},
                "Profile": {"type": "select", "select": {"name": "h"}},
                "Format": {"type": "select", "select": {"name": "audio"}},
                "Status": {"type": "select", "select": {"name": "pending"}},
                "Delete": {"type": "checkbox", "checkbox": False},
                "File Key": {"type": "rich_text", "rich_text": []},
            },
        }
        assert client._parse_page(page) is None


class TestPaginationBounds:
    """Notion can report has_more with a null next_cursor, which used to loop forever."""

    def test_has_more_with_null_cursor_stops(self, client, sample_page):
        response = {"results": [sample_page], "has_more": True, "next_cursor": None}

        with patch.object(client, "_post", return_value=response) as mock_post:
            entries = client.get_pending()

        assert mock_post.call_count == 1
        assert len(entries) == 1

    def test_null_cursor_stop_is_logged(self, client, caplog):
        import logging

        response = {"results": [], "has_more": True, "next_cursor": None}

        with (
            caplog.at_level(logging.WARNING, logger="mediasync.notion_client"),
            patch.object(client, "_post", return_value=response),
        ):
            client.get_pending()

        assert "no next_cursor" in caplog.text

    def test_page_count_is_capped(self, client):
        from mediasync.notion_client import MAX_QUERY_PAGES

        response = {"results": [], "has_more": True, "next_cursor": "always-more"}

        with patch.object(client, "_post", return_value=response) as mock_post:
            with pytest.raises(NotionError, match="safety limit"):
                client.get_pending()

        assert mock_post.call_count == MAX_QUERY_PAGES

    def test_normal_pagination_is_unaffected(self, client, sample_page):
        page2 = dict(sample_page)
        page2["id"] = "page-789"
        r1 = {"results": [sample_page], "has_more": True, "next_cursor": "cursor-1"}
        r2 = {"results": [page2], "has_more": False, "next_cursor": None}

        with patch.object(client, "_post", side_effect=[r1, r2]) as mock_post:
            entries = client.get_pending()

        assert mock_post.call_count == 2
        assert len(entries) == 2


class TestProcessedAndResetEntries:
    def test_get_processed_filters_done_and_failed(self, client, done_page):
        response = {"results": [done_page], "has_more": False, "next_cursor": None}
        with patch.object(client, "_post", return_value=response) as mock_post:
            entries = client.get_processed()

        assert len(entries) == 1
        filters = mock_post.call_args[0][1]["filter"]["and"]
        assert {"property": "Status", "select": {"equals": "done"}} in filters[0]["or"]
        assert {"property": "Status", "select": {"equals": "failed"}} in filters[0]["or"]
        assert {"property": "Delete", "checkbox": {"equals": False}} in filters

    def test_get_processed_raises_on_query_failure(self, client):
        with patch.object(client, "_post", return_value=None):
            with pytest.raises(NotionError, match="Failed to query processed"):
                client.get_processed()

    def test_reset_status_clears_processing_fields(self, client):
        with patch.object(client, "_patch", return_value={"id": "page-123"}) as mock_patch:
            client.reset_status("page-123")

        assert mock_patch.call_args.args[1]["properties"] == {
            "Status": {"select": None},
            "File Key": {"rich_text": []},
            "Duration": {"number": None},
            "Error": {"rich_text": []},
            "Processed At": {"date": None},
        }

    def test_reset_status_raises_on_patch_failure(self, client):
        with patch.object(client, "_patch", return_value=None):
            with pytest.raises(NotionError, match="Failed to reset Notion page"):
                client.reset_status("page-123")


class TestNotionErrorHandling:
    def test_update_status_raises_on_patch_failure(self, client):
        with patch.object(client, "_patch", return_value=None):
            with pytest.raises(NotionError, match="Failed to update Notion page"):
                client.update_status("page-123", Status.FAILED)

    def test_create_entry_without_id_returns_none(self, client):
        with patch.object(client, "_post", return_value={"object": "page"}):
            assert client.create_entry("https://example.test", "Harshit", Format.AUDIO) is None


def _ok(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.notion.com/v1/x", code, "err", {}, None)


class TestRequestSpacing:
    """Notion averages 3 req/s per integration; bursts used to trip 429s."""

    def test_back_to_back_requests_are_spaced(self, client):
        with (
            patch("mediasync.notion_client.time.monotonic", return_value=100.0),
            patch("mediasync.notion_client.time.sleep") as mock_sleep,
            patch("urllib.request.urlopen", side_effect=lambda *a, **k: _ok({})),
        ):
            client._get("https://api.notion.com/v1/a")
            client._get("https://api.notion.com/v1/b")
            client._get("https://api.notion.com/v1/c")

        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays == pytest.approx([MIN_REQUEST_INTERVAL_SECS, 2 * MIN_REQUEST_INTERVAL_SECS])

    def test_no_wait_once_the_interval_has_passed(self, client):
        clock = iter([100.0, 100.0 + MIN_REQUEST_INTERVAL_SECS + 1])
        with (
            patch("mediasync.notion_client.time.monotonic", side_effect=lambda: next(clock)),
            patch("mediasync.notion_client.time.sleep") as mock_sleep,
            patch("urllib.request.urlopen", side_effect=lambda *a, **k: _ok({})),
        ):
            client._get("https://api.notion.com/v1/a")
            client._get("https://api.notion.com/v1/b")

        mock_sleep.assert_not_called()


class TestTransientRetries:
    def test_429_then_success_is_retried(self, client):
        with patch("urllib.request.urlopen", side_effect=[_http_error(429), _ok({"id": "p1"})]) as mock_open:
            result = client._patch("https://api.notion.com/v1/pages/p1", {})

        assert result == {"id": "p1"}
        assert mock_open.call_count == 2

    def test_5xx_on_query_is_retried(self, client):
        with patch("urllib.request.urlopen", side_effect=[_http_error(502), _ok({"results": []})]) as mock_open:
            result = client._post("https://api.notion.com/v1/databases/db/query", {})

        assert result == {"results": []}
        assert mock_open.call_count == 2

    def test_client_error_is_not_retried(self, client):
        with patch("urllib.request.urlopen", side_effect=_http_error(400)) as mock_open:
            result = client._get("https://api.notion.com/v1/a")

        assert result is None
        assert mock_open.call_count == 1

    def test_persistent_failure_stops_after_bounded_attempts(self, client):
        with patch("urllib.request.urlopen", side_effect=_http_error(503)) as mock_open:
            result = client._get("https://api.notion.com/v1/a")

        assert result is None
        assert mock_open.call_count == REQUEST_ATTEMPTS


class TestCreateIsNotDuplicated:
    """A 5xx or timeout may arrive after Notion created the page."""

    @pytest.mark.parametrize("exc", [_http_error(503), TimeoutError("read timed out")])
    def test_create_is_not_retried_when_it_may_have_applied(self, client, exc):
        with patch("urllib.request.urlopen", side_effect=exc) as mock_open:
            page_id = client.create_entry("https://youtube.com/watch?v=abc", "harshit", Format.AUDIO)

        assert page_id is None
        assert mock_open.call_count == 1

    def test_create_is_retried_on_429(self, client):
        with patch("urllib.request.urlopen", side_effect=[_http_error(429), _ok({"id": "new"})]) as mock_open:
            page_id = client.create_entry("https://youtube.com/watch?v=abc", "harshit", Format.AUDIO)

        assert page_id == "new"
        assert mock_open.call_count == 2
