"""Retry utilities for MediaSync.

Provides exponential backoff retry for transient failures (network errors,
HTTP 429/5xx from YouTube, etc.).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 2.0  # seconds
DEFAULT_MAX_DELAY = 30.0  # seconds


def retry_on_error(
    fn: Callable[[], T],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    retryable: Callable[[Exception], bool] | None = None,
    description: str = "operation",
) -> T:
    """Execute fn with exponential backoff on failure.

    Args:
        fn: Zero-arg callable to execute.
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_delay: Initial delay in seconds (doubled each retry).
        max_delay: Cap on delay between retries.
        retryable: Predicate that decides if an exception is worth retrying.
                   Defaults to retrying all exceptions except KeyboardInterrupt.
        description: Human-readable label for log messages.

    Returns:
        The return value of fn on success.

    Raises:
        The last exception if all retries are exhausted.
    """
    if retryable is None:
        retryable = _default_retryable

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not retryable(exc) or attempt >= max_retries:
                raise

            delay = min(base_delay * (2 ** attempt), max_delay)
            logger.warning(
                "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                description, attempt + 1, max_retries + 1, delay, exc,
            )
            time.sleep(delay)

    # Should never reach here, but satisfies type checker
    raise last_exc  # type: ignore[misc]


def is_transient_download_error(exc: Exception) -> bool:
    """Determine if a download error is likely transient and worth retrying.

    Retries on: network errors, HTTP 429/5xx, timeouts, connection resets.
    Does NOT retry on: video unavailable, age-restricted, geo-blocked, format errors.
    """
    msg = str(exc).lower()

    # Definitely transient
    transient_indicators = [
        "429",
        "503",
        "502",
        "500",
        "connection reset",
        "connection refused",
        "timed out",
        "timeout",
        "temporary failure",
        "network",
        "ssl",
        "eof occurred",
        "incomplete read",
        "broken pipe",
    ]
    if any(indicator in msg for indicator in transient_indicators):
        return True

    # Definitely permanent — do not retry
    permanent_indicators = [
        "video unavailable",
        "private video",
        "removed",
        "age-restricted",
        "geo-restricted",
        "not available",
        "copyright",
        "terminated",
        "sign in",
        "members-only",
    ]
    if any(indicator in msg for indicator in permanent_indicators):
        return False

    # Default: retry (assume transient)
    return True


def _default_retryable(exc: Exception) -> bool:
    """Default predicate: retry everything except KeyboardInterrupt."""
    return True