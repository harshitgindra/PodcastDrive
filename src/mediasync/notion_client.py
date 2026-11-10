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
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import certifi

from retry import is_rate_limited, is_transient_http_error, retry_call

logger = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"

#: Hard cap on query pages (100 rows each) — a runaway-loop guard, not a limit
#: anyone should hit in practice.
MAX_QUERY_PAGES = 200
NOTION_VERSION = "2022-06-28"

#: Notion allows an average of 3 requests/second per integration; spacing calls
#: client-side avoids spending retries on 429s in the first place.
MIN_REQUEST_INTERVAL_SECS = 0.34
REQUEST_ATTEMPTS = 3

#: Notion rejects a rich_text segment longer than this (counted in UTF-16 code
#: units) with HTTP 400.
RICH_TEXT_SEGMENT_LIMIT = 2000
#: Page reads return at most this many rich_text segments per property, so a
#: longer value would be silently truncated on the next read.
RICH_TEXT_READ_SEGMENT_LIMIT = 25


class NotionError(RuntimeError):
    """Raised when a required Notion read or write fails."""


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
    #: Notion's own last_edited_time for the row, which is how a run tells a
    #: freshly claimed entry from one abandoned by a crashed run. None when
    #: Notion did not send a parseable timestamp.
    last_edited_at: datetime | None = None


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
        # Resolved lazily by _has_priority_column(); None means "not looked up yet".
        self._priority_sortable: bool | None = None
        self._throttle_lock = threading.Lock()
        self._next_request_at = 0.0

    def get_pending(self) -> list[MediaEntry]:
        """Fetch rows with Status=pending (or empty) and Delete=false.

        Sort order: Priority ascending when the database actually has a Priority
        number column, then created_time ascending. Failure to read the optional
        database schema only disables Priority sorting; query failures are fatal.

        Priority is an optional column (see the README).  This used to be handled
        by always sending the sort and retrying without it when Notion answered
        400 — which meant every run on a database without the column logged an
        ERROR and burned a wasted API call.  We now read the schema once per
        client and only send a sort Notion will accept.
        """
        filter_obj = {
            "and": [
                {
                    "or": [
                        {"property": "Status", "select": {"equals": "pending"}},
                        {"property": "Status", "select": {"equals": "downloading"}},
                        {"property": "Status", "select": {"is_empty": True}},
                    ]
                },
                {"property": "Delete", "checkbox": {"equals": False}},
            ]
        }
        sorts: list[dict[str, str]] = []
        if self._has_priority_column():
            sorts.append({"property": "Priority", "direction": "ascending"})
        sorts.append({"timestamp": "created_time", "direction": "ascending"})
        result = self._query(filter_obj=filter_obj, sorts=sorts)
        if result is None:
            raise NotionError("Failed to query pending MediaSync entries")
        return result

    def _has_priority_column(self) -> bool:
        """True when the database has a sortable Priority number column.

        Cached for the client's lifetime: the schema cannot change mid-run, and a
        run does several get_pending() calls.  On any failure to read the schema we
        answer False, which costs correct ordering but never breaks the query.
        """
        if self._priority_sortable is None:
            schema = self._get(f"{NOTION_API}/databases/{self._db_id}")
            if schema is None:
                logger.warning("Could not read Notion database schema; skipping optional Priority sort")
                self._priority_sortable = False
                return False
            prop = schema.get("properties", {}).get("Priority", {})
            self._priority_sortable = prop.get("type") == "number"
            if not self._priority_sortable:
                logger.debug("No Priority number column on this database — sorting by created_time only")
        return self._priority_sortable

    def get_deletions(self) -> list[MediaEntry]:
        """Fetch rows marked for deletion that have been processed."""
        result = self._query(
            filter_obj={
                "and": [
                    {"property": "Delete", "checkbox": {"equals": True}},
                    {"property": "Status", "select": {"equals": "done"}},
                ]
            }
        )
        if result is None:
            raise NotionError("Failed to query MediaSync deletions")
        return result

    def get_done_for_profile(self, profile: str) -> list[MediaEntry]:
        """Fetch all done entries for a profile (for deduplication)."""
        result = self._query(
            filter_obj={
                "and": [
                    {"property": "Profile", "select": {"equals": profile}},
                    {"property": "Status", "select": {"equals": "done"}},
                    {"property": "Delete", "checkbox": {"equals": False}},
                ]
            }
        )
        if result is None:
            raise NotionError(f"Failed to query done MediaSync entries for profile {profile!r}")
        return result

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
            segments = _split_rich_text(file_key)
            if len(segments) > RICH_TEXT_READ_SEGMENT_LIMIT:
                raise NotionError(
                    f"File Key for Notion page {page_id} is {len(file_key)} chars; "
                    f"more than {RICH_TEXT_READ_SEGMENT_LIMIT} segments cannot be read back intact"
                )
            properties["File Key"] = {"rich_text": [{"text": {"content": seg}} for seg in segments]}
        if duration is not None:
            properties["Duration"] = {"number": duration}
        if error is not None:
            properties["Error"] = {"rich_text": [{"text": {"content": _split_rich_text(error)[0]}}]}
        if status == Status.DONE:
            properties["Processed At"] = {"date": {"start": datetime.now(timezone.utc).isoformat()}}

        if self._patch(f"{NOTION_API}/pages/{page_id}", {"properties": properties}) is None:
            raise NotionError(f"Failed to update Notion page {page_id}")

    def get_processed(self) -> list[MediaEntry]:
        """Fetch all rows with Status=done or Status=failed (for reset)."""
        result = self._query(
            filter_obj={
                "and": [
                    {
                        "or": [
                            {"property": "Status", "select": {"equals": "done"}},
                            {"property": "Status", "select": {"equals": "failed"}},
                        ]
                    },
                    {"property": "Delete", "checkbox": {"equals": False}},
                ]
            }
        )
        if result is None:
            raise NotionError("Failed to query processed MediaSync entries")
        return result

    def reset_status(self, page_id: str) -> None:
        """Clear status and processing metadata so the entry is re-processed."""
        result = self._patch(
            f"{NOTION_API}/pages/{page_id}",
            {
                "properties": {
                    "Status": {"select": None},
                    "File Key": {"rich_text": []},
                    "Duration": {"number": None},
                    "Error": {"rich_text": []},
                    "Processed At": {"date": None},
                }
            },
        )
        if result is None:
            raise NotionError(f"Failed to reset Notion page {page_id}")

    def archive_page(self, page_id: str) -> None:
        """Archive a page after deletion processing."""
        if self._patch(f"{NOTION_API}/pages/{page_id}", {"archived": True}) is None:
            raise NotionError(f"Failed to archive Notion page {page_id}")

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
        result = self._post(f"{NOTION_API}/pages", payload, idempotent=False)
        if result and "id" in result:
            return result["id"]
        return None

    def get_all_for_profile(self, profile: str) -> list[MediaEntry]:
        """Fetch all non-deleted entries for a profile (any status)."""
        result = self._query(
            filter_obj={
                "and": [
                    {"property": "Profile", "select": {"equals": profile}},
                    {"property": "Delete", "checkbox": {"equals": False}},
                ]
            }
        )
        if result is None:
            raise NotionError(f"Failed to query MediaSync entries for profile {profile!r}")
        return result

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
            raise NotionError(
                f"Notion query exceeded the {MAX_QUERY_PAGES}-page safety limit; refusing incomplete results"
            )

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

        file_key = "".join(seg.get("plain_text", "") for seg in props.get("File Key", {}).get("rich_text") or [])

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
            last_edited_at=_parse_timestamp(page.get("last_edited_time")),
        )

    def _wait_for_slot(self) -> None:
        with self._throttle_lock:
            now = time.monotonic()
            delay = self._next_request_at - now
            self._next_request_at = max(now, self._next_request_at) + MIN_REQUEST_INTERVAL_SECS
        if delay > 0:
            time.sleep(delay)

    def _send(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotent: bool = True,
    ) -> dict[str, Any]:
        """Send one rate-limited request, retrying transient failures.

        A non-idempotent request is retried only on 429: after a 5xx or a
        timeout Notion may already have applied it, and a blind retry of a
        page create would add a duplicate row.
        """
        data = json.dumps(payload).encode("utf-8") if payload is not None else None

        def attempt() -> dict[str, Any]:
            self._wait_for_slot()
            req = urllib.request.Request(url, data=data, headers=self._headers, method=method)
            with urllib.request.urlopen(req, timeout=30, context=self._ssl_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))

        return retry_call(
            attempt,
            attempts=REQUEST_ATTEMPTS,
            base_delay=1.0,
            jitter=True,
            retryable=is_transient_http_error if idempotent else is_rate_limited,
            label=f"notion {method} {url}",
            logger=logger,
        )

    def _post(self, url: str, payload: dict[str, Any], *, idempotent: bool = True) -> dict[str, Any] | None:
        """Make a POST request to Notion API."""
        try:
            return self._send("POST", url, payload, idempotent=idempotent)
        except Exception as exc:
            logger.error("Notion POST %s failed: %s", url, _describe_error(exc))
            return None

    def _get(self, url: str) -> dict[str, Any] | None:
        """Make a GET request to Notion API."""
        try:
            return self._send("GET", url)
        except Exception as exc:
            logger.warning("Notion GET %s failed: %s", url, _describe_error(exc))
            return None

    def _patch(self, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Make a PATCH request to Notion API."""
        try:
            return self._send("PATCH", url, payload)
        except Exception as exc:
            logger.error("Notion PATCH %s failed: %s", url, _describe_error(exc))
            return None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a Notion ISO-8601 timestamp, tolerating the trailing Z form."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _split_rich_text(text: str) -> list[str]:
    """Split text into segments Notion accepts; joining them restores the text."""
    segments: list[str] = []
    current: list[str] = []
    units = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > RICH_TEXT_SEGMENT_LIMIT:
            segments.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += width
    segments.append("".join(current))
    return segments


def _describe_error(exc: Exception) -> str:
    """Include Notion's response body: a bare "400: Bad Request" hides which field was rejected."""
    if not isinstance(exc, urllib.error.HTTPError):
        return str(exc)
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    return f"{exc} — {body[:1000]}" if body else str(exc)
