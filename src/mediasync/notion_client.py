"""Notion API client for the MediaSync database.

Uses urllib (no requests dependency) to stay consistent with the existing
PodcastDrive codebase.

Database schema:
    - URL (url): YouTube link
    - Profile (select): profile name
    - Format (select): audio / video / both
    - Status (select): pending / downloading / done / failed
    - Delete (checkbox): soft-delete flag
    - File Key (rich_text): pCloud path(s) after upload
    - Duration (number): seconds
    - Processed At (date): completion timestamp
    - Error (rich_text): failure reason
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import certifi

logger = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"

#: Hard cap on query pages (100 rows each) — a runaway-loop guard, not a limit
#: anyone should hit in practice.
MAX_QUERY_PAGES = 200
NOTION_VERSION = "2022-06-28"


class Format(Enum):
    AUDIO = "audio"
    VIDEO = "video"
    BOTH = "both"


class Status(Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"


@dataclass
class MediaEntry:
    page_id: str
    url: str
    profile: str
    format: Format
    status: Status
    delete: bool
    file_key: str = ""
    priority: int | None = None


class NotionClient:
    """Client for querying and updating the MediaSync Notion database."""

    def __init__(self, token: str, database_id: str) -> None:
        self._token = token
        self._db_id = database_id
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    def get_pending(self) -> list[MediaEntry]:
        """Fetch rows with Status=pending (or empty) and Delete=false.

        Sort order: Priority ascending (nulls last), then created_time ascending.
        Falls back to created_time only if Priority property doesn't exist.
        """
        filter_obj = {
            "and": [
                {"or": [
                    {"property": "Status", "select": {"equals": "pending"}},
                    {"property": "Status", "select": {"equals": "downloading"}},
                    {"property": "Status", "select": {"is_empty": True}},
                ]},
                {"property": "Delete", "checkbox": {"equals": False}},
            ]
        }
        result = self._query(
            filter_obj=filter_obj,
            sorts=[
                {"property": "Priority", "direction": "ascending"},
                {"timestamp": "created_time", "direction": "ascending"},
            ],
        )
        if result is not None:
            return result
        # Priority property may not exist; retry without it
        logger.info("Retrying get_pending without Priority sort")
        return self._query(
            filter_obj=filter_obj,
            sorts=[{"timestamp": "created_time", "direction": "ascending"}],
        ) or []

    def get_deletions(self) -> list[MediaEntry]:
        """Fetch rows marked for deletion that have been processed."""
        return self._query(
            filter_obj={
                "and": [
                    {"property": "Delete", "checkbox": {"equals": True}},
                    {"property": "Status", "select": {"equals": "done"}},
                ]
            }
        ) or []

    def get_done_for_profile(self, profile: str) -> list[MediaEntry]:
        """Fetch all done entries for a profile (for deduplication)."""
        return self._query(
            filter_obj={
                "and": [
                    {"property": "Profile", "select": {"equals": profile}},
                    {"property": "Status", "select": {"equals": "done"}},
                    {"property": "Delete", "checkbox": {"equals": False}},
                ]
            }
        ) or []

    def update_status(
        self,
        page_id: str,
        status: Status,
        *,
        file_key: str | None = None,
        duration: int | None = None,
        error: str | None = None,
    ) -> None:
        """Update a Notion page with processing results."""
        properties: dict[str, Any] = {
            "Status": {"select": {"name": status.value}},
        }
        if file_key is not None:
            properties["File Key"] = {"rich_text": [{"text": {"content": file_key}}]}
        if duration is not None:
            properties["Duration"] = {"number": duration}
        if error is not None:
            properties["Error"] = {"rich_text": [{"text": {"content": error[:2000]}}]}
        if status == Status.DONE:
            properties["Processed At"] = {
                "date": {"start": datetime.now(timezone.utc).isoformat()}
            }

        self._patch(f"{NOTION_API}/pages/{page_id}", {"properties": properties})

    def get_processed(self) -> list[MediaEntry]:
        """Fetch all rows with Status=done or Status=failed (for reset)."""
        return self._query(
            filter_obj={
                "and": [
                    {"or": [
                        {"property": "Status", "select": {"equals": "done"}},
                        {"property": "Status", "select": {"equals": "failed"}},
                    ]},
                    {"property": "Delete", "checkbox": {"equals": False}},
                ]
            }
        ) or []

    def reset_status(self, page_id: str) -> None:
        """Clear status and processing metadata so the entry is re-processed."""
        self._patch(f"{NOTION_API}/pages/{page_id}", {
            "properties": {
                "Status": {"select": None},
                "File Key": {"rich_text": []},
                "Duration": {"number": None},
                "Error": {"rich_text": []},
                "Processed At": {"date": None},
            }
        })

    def archive_page(self, page_id: str) -> None:
        """Archive a page after deletion processing."""
        self._patch(f"{NOTION_API}/pages/{page_id}", {"archived": True})

    def create_entry(self, url: str, profile: str, fmt: Format) -> str | None:
        """Create a new pending entry in Notion. Returns page_id or None on failure."""
        payload = {
            "parent": {"database_id": self._db_id},
            "properties": {
                "Name": {"title": [{"text": {"content": url}}]},
                "Profile": {"select": {"name": profile}},
                "Format": {"select": {"name": fmt.value}},
                "Status": {"select": {"name": Status.PENDING.value}},
                "Delete": {"checkbox": False},
            },
        }
        result = self._post(f"{NOTION_API}/pages", payload)
        if result and "id" in result:
            return result["id"]
        return None

    def get_all_for_profile(self, profile: str) -> list[MediaEntry]:
        """Fetch all non-deleted entries for a profile (any status)."""
        return self._query(
            filter_obj={
                "and": [
                    {"property": "Profile", "select": {"equals": profile}},
                    {"property": "Delete", "checkbox": {"equals": False}},
                ]
            }
        ) or []

    def _query(
        self,
        filter_obj: dict[str, Any],
        sorts: list[dict[str, str]] | None = None,
    ) -> list[MediaEntry] | None:
        """Query the database with pagination. Returns None on API failure."""
        entries: list[MediaEntry] = []
        payload: dict[str, Any] = {"filter": filter_obj}
        if sorts:
            payload["sorts"] = sorts

        has_more = True
        start_cursor: str | None = None
        # Bounded on both a null next_cursor and an absolute page count: Notion
        # can return has_more=True with next_cursor=None, and re-POSTing without
        # a cursor re-fetches page 1 forever.
        page_num = 0

        while has_more and page_num < MAX_QUERY_PAGES:
            page_num += 1
            if start_cursor:
                payload["start_cursor"] = start_cursor

            data = self._post(f"{NOTION_API}/databases/{self._db_id}/query", payload)
            if data is None:
                if page_num == 1:
                    return None  # Signal query failure (e.g. invalid sort property)
                break

            for page in data.get("results", []):
                entry = self._parse_page(page)
                if entry:
                    entries.append(entry)

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")
            if has_more and not start_cursor:
                logger.warning(
                    "Notion reported has_more with no next_cursor after page %d — stopping pagination",
                    page_num,
                )
                break

        if has_more and page_num >= MAX_QUERY_PAGES:
            logger.warning("Notion pagination hit the %d-page limit — results may be truncated", MAX_QUERY_PAGES)

        return entries

    def _parse_page(self, page: dict[str, Any]) -> MediaEntry | None:
        """Parse a Notion page into a MediaEntry."""
        props = page.get("properties", {})
        try:
            # URL is in the Name (title) column
            name_prop = props.get("Name", {})
            url = ""
            if name_prop.get("type") == "title":
                title_items = name_prop.get("title", [])
                url = title_items[0]["plain_text"].strip() if title_items else ""

            if not url:
                return None

            profile = props["Profile"]["select"]["name"]
            fmt = Format(props["Format"]["select"]["name"].lower())

            # Status can be null (treat as pending)
            status_select = props.get("Status", {}).get("select")
            if status_select and status_select.get("name"):
                status = Status(status_select["name"].lower())
            else:
                status = Status.PENDING

            delete = props["Delete"]["checkbox"]
        except (KeyError, TypeError, ValueError):
            return None

        file_key = ""
        file_key_prop = props.get("File Key", {})
        if file_key_prop.get("rich_text"):
            file_key = file_key_prop["rich_text"][0]["plain_text"]

        # Priority is optional (number column, nullable)
        priority = None
        priority_prop = props.get("Priority", {})
        if priority_prop.get("type") == "number" and priority_prop.get("number") is not None:
            priority = int(priority_prop["number"])

        return MediaEntry(
            page_id=page["id"],
            url=url,
            profile=profile,
            format=fmt,
            status=status,
            delete=delete,
            file_key=file_key,
            priority=priority,
        )

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Make a POST request to Notion API."""
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ssl_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.error("Notion POST %s failed: %s", url, exc)
            return None

    def _patch(self, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Make a PATCH request to Notion API."""
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers,
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ssl_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.error("Notion PATCH %s failed: %s", url, exc)
            return None
