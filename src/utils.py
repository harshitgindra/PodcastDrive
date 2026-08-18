"""Utility functions for YouTube Playlist to Podcast."""

import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar
from urllib.parse import parse_qs, urlparse

from retry import RETRYABLE_AWS_CODES, is_transient_aws_error, retry_call

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AWS retry helper
# ---------------------------------------------------------------------------

#: Backwards-compatible alias. The canonical set now lives in ``retry`` so the
#: MediaSync and podcast paths cannot disagree about what is transient.
_RETRYABLE_CODES = RETRYABLE_AWS_CODES

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

    Thin wrapper over :func:`retry.retry_call` with the AWS predicate and
    jitter enabled; kept as-is because ~23 call sites depend on this signature.
    Jitter matters here because several of those sites run inside a
    ``ThreadPoolExecutor``, and un-jittered back-off would resynchronise the
    workers into the same throttling wall on every attempt.

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
    return retry_call(
        fn,
        attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
        jitter=True,
        retryable=is_transient_aws_error,
        label=label,
        logger=_logger,
    )


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
            f"Playlist ID contains unsafe characters: {playlist_id!r} (only alphanumeric, @, ., _, - are allowed)"
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
        A :class:`datetime` with UTC timezone. Falls back to epoch (1970-01-01)
        if *date_str* is not a valid YYYYMMDD string — this ensures episodes
        with missing dates are caught by age filtering rather than appearing
        as the newest.
    """
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        return dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tolerant environment-variable parsing
# ---------------------------------------------------------------------------


def env_int(name: str, default: int) -> int:
    """Read integer environment variable *name*, falling back to *default*.

    A malformed value (typo, stray whitespace, empty string) logs a warning and
    yields *default* instead of raising ``ValueError`` mid-run.  Configuration
    mistakes should degrade to documented defaults, not abort a sync that has
    already paid for downloads and transcription.

    Args:
        name:    Environment variable name.
        default: Value to use when unset or unparseable.

    Returns:
        The parsed integer, or *default*.

    Note:
        New podcast-pipeline settings belong in :mod:`settings`, which declares a
        name, type, default and documentation once and renders
        ``config.env.example``.  These helpers remain for :mod:`src.mediasync`,
        which keeps its own typed ``Config`` and ``mediasync.env.example``.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        _logger.warning(
            "Invalid %s=%r (expected an integer) — falling back to %d",
            name,
            raw,
            default,
        )
        return default


def env_float(name: str, default: float) -> float:
    """Read float environment variable *name*, falling back to *default*.

    See :func:`env_int` for the rationale.

    Args:
        name:    Environment variable name.
        default: Value to use when unset or unparseable.

    Returns:
        The parsed float, or *default*.

    Note:
        New podcast-pipeline settings belong in :mod:`settings`, which declares a
        name, type, default and documentation once and renders
        ``config.env.example``.  These helpers remain for :mod:`src.mediasync`,
        which keeps its own typed ``Config`` and ``mediasync.env.example``.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        _logger.warning(
            "Invalid %s=%r (expected a number) — falling back to %s",
            name,
            raw,
            default,
        )
        return default
