"""Utility functions for YouTube Playlist to Podcast."""

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar
from urllib.parse import parse_qs, urlparse

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AWS retry helper
# ---------------------------------------------------------------------------

#: Error codes that indicate a transient AWS service issue and are safe to retry.
_RETRYABLE_CODES: frozenset[str] = frozenset({
    "Throttling",
    "ThrottlingException",
    "RequestLimitExceeded",
    "RequestThrottled",
    "ProvisionedThroughputExceededException",
    "TransactionInProgressException",
    "ServiceUnavailable",
    "InternalServerError",
    "InternalFailure",
    "RequestExpired",
    "SlowDown",
    "EC2ThrottledException",
})

_T = TypeVar("_T")


def retry_aws_call(
    fn: Callable[[], _T],
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 32.0,
    label: str = "",
) -> _T:
    """Call *fn* and retry on transient AWS / network errors with exponential back-off.

    Uses full-jitter exponential back-off: ``sleep = min(max_delay, base_delay * 2**attempt)``.

    Args:
        fn:           Zero-argument callable that performs the AWS call.
        max_attempts: Maximum number of total attempts (default: 5).
        base_delay:   Initial delay in seconds (doubles each attempt, default: 1.0).
        max_delay:    Cap on the sleep duration in seconds (default: 32.0).
        label:        Short human-readable label for log messages (e.g. ``"s3.put_object"``).

    Returns:
        The return value of *fn* on success.

    Raises:
        The last exception if all attempts are exhausted.
    """
    import random

    from botocore.exceptions import ClientError, EndpointResolutionError

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in _RETRYABLE_CODES:
                raise  # non-transient — propagate immediately
            last_exc = exc
        except (ConnectionError, OSError, EndpointResolutionError) as exc:
            last_exc = exc

        delay = min(max_delay, base_delay * (2 ** attempt))
        jitter = random.uniform(0, delay * 0.5)
        sleep_time = min(max_delay, delay + jitter)
        _logger.warning(
            "Transient AWS error on %s (attempt %d/%d): %s — retrying in %.1fs",
            label or getattr(fn, "__name__", repr(fn)), attempt + 1, max_attempts, last_exc, sleep_time,
        )
        time.sleep(sleep_time)

    raise last_exc  # type: ignore[misc]


_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9@._-]+$")


def _validate_playlist_id(playlist_id: str) -> str:
    """Validate that a playlist/channel ID contains only safe characters.

    Prevents path traversal (e.g. ``../``) or other unsafe characters
    from being used in S3 keys or filesystem paths.

    Raises:
        ValueError: If the ID contains unsafe characters.
    """
    if not playlist_id:
        raise ValueError("Playlist ID is empty")
    if not _SAFE_ID_RE.match(playlist_id):
        raise ValueError(
            f"Playlist ID contains unsafe characters: {playlist_id!r} "
            "(only alphanumeric, @, ., _, - are allowed)"
        )
    if ".." in playlist_id:
        raise ValueError(f"Playlist ID contains path traversal: {playlist_id!r}")
    return playlist_id


def extract_playlist_id(url: str) -> str:
    """Extract a playlist or channel ID from a YouTube URL.

    Supports:
    - Playlist URLs: ``https://www.youtube.com/playlist?list=PLxyz``
    - Channel URLs: ``https://www.youtube.com/@Handle/videos``
    - Channel URLs: ``https://www.youtube.com/channel/UCxyz``
    - Raw IDs: ``PLxyz``, ``UCxyz``, ``UUxyz``

    For channel URLs, yt_dlp returns the channel ID (``UCxyz``) which is
    used directly as the S3 prefix.

    Args:
        url: YouTube playlist URL, channel URL, or raw ID.

    Returns:
        The playlist or channel ID string.

    Raises:
        ValueError: If no ID can be extracted or contains unsafe characters.
    """
    # Already a raw ID (no URL scheme)
    if not url.startswith("http"):
        return _validate_playlist_id(url)

    parsed = urlparse(url)

    # Playlist URL: ?list=PLxyz
    params = parse_qs(parsed.query)
    playlist_id = params.get("list", [None])[0]
    if playlist_id:
        return _validate_playlist_id(playlist_id)

    # Channel URL: /channel/UCxyz
    match = re.search(r"/channel/(UC[a-zA-Z0-9_-]+)", parsed.path)
    if match:
        return _validate_playlist_id(match.group(1))

    # Handle URL: /@Handle or /@Handle/videos
    match = re.search(r"/@([a-zA-Z0-9_.-]+)", parsed.path)
    if match:
        # Use the handle as the ID — yt_dlp will resolve it
        return _validate_playlist_id(f"@{match.group(1)}")

    raise ValueError(f"Could not extract playlist or channel ID from URL: {url}")


def parse_upload_date(date_str: str) -> datetime:
    """Parse a YYYYMMDD date string into a timezone-aware datetime.

    Args:
        date_str: Date string in YYYYMMDD format (e.g. ``"20250101"``).

    Returns:
        A :class:`datetime` with UTC timezone. Falls back to today's UTC date
        if *date_str* is not a valid YYYYMMDD string.
    """
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        return dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        now = datetime.now(UTC)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
