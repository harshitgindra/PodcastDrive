"""Tests for resumability and playlist sync integration."""

from unittest.mock import MagicMock, patch

import pytest

from mediasync.config import Config, Profile
from mediasync.downloader import DownloadError
from mediasync.notion_client import Format, MediaEntry, NotionClient, Status
from mediasync.pipeline import run, RunStats


@pytest.fixture
def config():
    return Config(
        notion_token="token",
        notion_database_id="db-id",
        s3_bucket="test-bucket",
        s3_region="us-west-2",
        s3_prefix="MediaSync",
        profiles=[Profile("Harshit")],
        max_duration_secs=7200,
        output_dir="/tmp/mediasync",
        herald_enabled=False,
        herald_job_id="mediasync",
    )


def _entry(url, status=Status.PENDING, profile="Harshit", fmt=Format.AUDIO, file_key=""):
    return MediaEntry(
        page_id=f"page-{hash(url) % 10000}",
        url=url,
        profile=profile,
        format=fmt,
        status=status,
        delete=False,
        file_key=file_key,
    )


class TestResumability:
    """Entries stuck in downloading state should be retried on next run."""

    def test_get_pending_includes_downloading_status(self):
        """The Notion query filter includes downloading status."""
        client = NotionClient("token", "db-id")
        downloading_page = {
            "id": "page-stuck",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "https://youtube.com/watch?v=stuck"}]},
                "Profile": {"type": "select", "select": {"name": "Harshit"}},
                "Format": {"type": "select", "select": {"name": "audio"}},
                "Status": {"type": "select", "select": {"name": "downloading"}},
                "Delete": {"type": "checkbox", "checkbox": False},
                "File Key": {"type": "rich_text", "rich_text": []},
            },
        }
        response = {"results": [downloading_page], "has_more": False, "next_cursor": None}

        with patch.object(client, "_post", return_value=response) as mock_post:
            entries = client.get_pending()

        assert len(entries) == 1
        assert entries[0].status == Status.DOWNLOADING
        payload = mock_post.call_args[0][1]
        status_filter = payload["filter"]["and"][0]["or"]
        assert {"property": "Status", "select": {"equals": "downloading"}} in status_filter

    def test_get_pending_filter_includes_all_resumable_states(self):
        """Filter should have pending, downloading, and is_empty."""
        client = NotionClient("token", "db-id")
        response = {"results": [], "has_more": False, "next_cursor": None}

        with patch.object(client, "_post", return_value=response) as mock_post:
            client.get_pending()

        payload = mock_post.call_args[0][1]
        status_filter = payload["filter"]["and"][0]["or"]
        assert len(status_filter) == 3
        expected = [
            {"property": "Status", "select": {"equals": "pending"}},
            {"property": "Status", "select": {"equals": "downloading"}},
            {"property": "Status", "select": {"is_empty": True}},
        ]
        for item in expected:
            assert item in status_filter

    @patch("mediasync.pipeline.sync_playlists", return_value=0)
    @patch("mediasync.pipeline._process_entry", return_value=True)
    @patch("mediasync.pipeline._is_duplicate", return_value=False)
    @patch("mediasync.pipeline.create_storage")
    @patch("mediasync.pipeline.NotionClient")
    def test_interrupted_entry_gets_retried(
        self, MockNotion, MockStorage, mock_dup, mock_proc, mock_sync, config
    ):
        """An entry stuck in downloading state is picked up and processed."""
        stuck_entry = _entry("https://youtube.com/watch?v=interrupted", status=Status.DOWNLOADING)
        mock_notion = MockNotion.return_value
        mock_notion.get_deletions.return_value = []
        mock_notion.get_pending.return_value = [stuck_entry]

        stats = run(config)

        assert stats.processed == 1
        mock_proc.assert_called_once()


class TestGetPendingPriorityFallback:
    """get_pending falls back to created_time sort if Priority property does not exist."""

    def test_fallback_on_first_query_failure(self):
        client = NotionClient("token", "db-id")
        response = {"results": [], "has_more": False, "next_cursor": None}

        with patch.object(client, "_post", side_effect=[None, response]) as mock_post:
            entries = client.get_pending()

        assert entries == []
        assert mock_post.call_count == 2
        second_payload = mock_post.call_args_list[1][0][1]
        sorts = second_payload["sorts"]
        assert len(sorts) == 1
        assert sorts[0] == {"timestamp": "created_time", "direction": "ascending"}

    def test_no_fallback_when_first_query_succeeds(self):
        client = NotionClient("token", "db-id")
        page = {
            "id": "page-1",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "https://youtube.com/watch?v=x"}]},
                "Profile": {"type": "select", "select": {"name": "Harshit"}},
                "Format": {"type": "select", "select": {"name": "audio"}},
                "Status": {"type": "select", "select": {"name": "pending"}},
                "Delete": {"type": "checkbox", "checkbox": False},
                "File Key": {"type": "rich_text", "rich_text": []},
            },
        }
        response = {"results": [page], "has_more": False, "next_cursor": None}

        with patch.object(client, "_post", return_value=response) as mock_post:
            entries = client.get_pending()

        assert len(entries) == 1
        assert mock_post.call_count == 1


class TestCreateEntry:
    """NotionClient.create_entry creates new pending Notion pages."""

    def test_creates_page_with_correct_properties(self):
        client = NotionClient("token", "db-id")
        with patch.object(client, "_post", return_value={"id": "new-page-123"}) as mock_post:
            page_id = client.create_entry(
                "https://youtube.com/watch?v=new", "Harshit", Format.AUDIO
            )

        assert page_id == "new-page-123"
        payload = mock_post.call_args[0][1]
        assert payload["parent"] == {"database_id": "db-id"}
        props = payload["properties"]
        assert props["Name"]["title"][0]["text"]["content"] == "https://youtube.com/watch?v=new"
        assert props["Profile"]["select"]["name"] == "Harshit"
        assert props["Format"]["select"]["name"] == "audio"
        assert props["Status"]["select"]["name"] == "pending"
        assert props["Delete"]["checkbox"] is False

    def test_returns_none_on_failure(self):
        client = NotionClient("token", "db-id")
        with patch.object(client, "_post", return_value=None):
            page_id = client.create_entry(
                "https://youtube.com/watch?v=fail", "Harshit", Format.VIDEO
            )
        assert page_id is None

    def test_video_format(self):
        client = NotionClient("token", "db-id")
        with patch.object(client, "_post", return_value={"id": "vid-page"}) as mock_post:
            client.create_entry("https://youtube.com/watch?v=v", "Dishita", Format.VIDEO)

        props = mock_post.call_args[0][1]["properties"]
        assert props["Format"]["select"]["name"] == "video"
        assert props["Profile"]["select"]["name"] == "Dishita"


class TestGetAllForProfile:
    """NotionClient.get_all_for_profile returns all non-deleted entries."""

    def test_returns_entries_any_status(self):
        client = NotionClient("token", "db-id")
        pages = [
            {
                "id": f"page-{i}",
                "properties": {
                    "Name": {"type": "title", "title": [{"plain_text": f"https://youtube.com/watch?v={i}"}]},
                    "Profile": {"type": "select", "select": {"name": "Harshit"}},
                    "Format": {"type": "select", "select": {"name": "audio"}},
                    "Status": {"type": "select", "select": {"name": status}},
                    "Delete": {"type": "checkbox", "checkbox": False},
                    "File Key": {"type": "rich_text", "rich_text": []},
                },
            }
            for i, status in enumerate(["pending", "downloading", "done", "failed"])
        ]
        response = {"results": pages, "has_more": False, "next_cursor": None}

        with patch.object(client, "_post", return_value=response) as mock_post:
            entries = client.get_all_for_profile("Harshit")

        assert len(entries) == 4
        payload = mock_post.call_args[0][1]
        filters = payload["filter"]["and"]
        assert {"property": "Profile", "select": {"equals": "Harshit"}} in filters
        assert {"property": "Delete", "checkbox": {"equals": False}} in filters

    def test_returns_empty_on_failure(self):
        client = NotionClient("token", "db-id")
        with patch.object(client, "_post", return_value=None):
            entries = client.get_all_for_profile("Harshit")
        assert entries == []


class TestPlaylistSyncIntegration:
    """Playlist sync runs as part of the pipeline."""

    @patch("mediasync.pipeline.sync_playlists")
    @patch("mediasync.pipeline.create_storage")
    @patch("mediasync.pipeline.NotionClient")
    def test_sync_runs_before_pending(self, MockNotion, MockStorage, mock_sync, config):
        """Playlist sync runs so new entries are available for the pending phase."""
        mock_notion = MockNotion.return_value
        mock_notion.get_deletions.return_value = []
        mock_notion.get_pending.return_value = []
        mock_sync.return_value = 3

        stats = run(config)

        mock_sync.assert_called_once_with(mock_notion, ["Harshit"])
        assert stats.processed == 0

    @patch("mediasync.pipeline.sync_playlists")
    @patch("mediasync.pipeline._process_entry", return_value=True)
    @patch("mediasync.pipeline._is_duplicate", return_value=False)
    @patch("mediasync.pipeline.create_storage")
    @patch("mediasync.pipeline.NotionClient")
    def test_new_entries_from_sync_are_processed(
        self, MockNotion, MockStorage, mock_dup, mock_proc, mock_sync, config
    ):
        """Entries created by playlist sync show up in get_pending and get processed."""
        mock_sync.return_value = 2
        new_entry = _entry("https://youtube.com/watch?v=synced")
        mock_notion = MockNotion.return_value
        mock_notion.get_deletions.return_value = []
        mock_notion.get_pending.return_value = [new_entry]

        stats = run(config)

        assert stats.processed == 1
        mock_proc.assert_called_once()
