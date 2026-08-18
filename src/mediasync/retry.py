"""Retry entry point for MediaSync.

The engine and the transient/permanent classification now live in the
top-level :mod:`retry` module, shared with the podcast pipeline. This module
stays as MediaSync's entry point — its call sites and defaults are unchanged —
but no longer carries its own copy of the loop.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from retry import is_transient_download_error, retry_call

__all__ = [
    "DEFAULT_BASE_DELAY",
    "DEFAULT_MAX_DELAY",
    "DEFAULT_MAX_RETRIES",
    "is_transient_download_error",
    "retry_on_error",
]

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
        max_retries: Maximum number of *retries* after the initial attempt
            (0 = no retries), so the total attempt count is ``max_retries + 1``.
        base_delay: Initial delay in seconds (doubled each retry).
        max_delay: Cap on delay between retries.
        retryable: Predicate that decides if an exception is worth retrying.
                   Defaults to retrying every exception.
        description: Human-readable label for log messages.

    Returns:
        The return value of fn on success.

    Raises:
        The last exception if all retries are exhausted.
    """
    return retry_call(
        fn,
        attempts=max_retries + 1,
        base_delay=base_delay,
        max_delay=max_delay,
        jitter=False,
        retryable=retryable,  # type: ignore[arg-type]
        label=description,
    )
