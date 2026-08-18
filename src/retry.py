"""The single retry engine, plus the predicates that decide what is worth retrying.

There used to be three separate implementations:

  * ``utils.retry_aws_call`` — botocore-aware, jittered, allow-listed error codes
  * ``mediasync.retry.retry_on_error`` — generic, un-jittered, pluggable predicate
  * a ``tenacity`` decorator in ``downloader.py`` — retried *every* exception,
    so a permanently unavailable video still burned every attempt and its
    back-off waits before failing

They disagreed on whether to sleep before giving up, on jitter, and above all
on what counts as transient. This module keeps the one loop and exposes the
policy as predicates, so each caller states its own definition of transient
rather than reimplementing the loop around it.

Callers keep their existing entry points (:func:`utils.retry_aws_call` and
:func:`mediasync.retry.retry_on_error` are thin wrappers over :func:`retry_call`)
so no call site had to change.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_logger = logging.getLogger(__name__)

#: AWS error codes worth retrying. Anything else (AccessDenied,
#: NoSuchBucket, ValidationException, ...) is a real error that a retry
#: cannot fix, so it propagates on the first attempt.
RETRYABLE_AWS_CODES: frozenset[str] = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "RequestThrottled",
        "RequestThrottledException",
        "TooManyRequestsException",
        "ProvisionedThroughputExceededException",
        "TransactionInProgressException",
        "RequestLimitExceeded",
        "RequestTimeout",
        "RequestTimeoutException",
        "PriorRequestNotComplete",
        "ConnectionError",
        "HttpTimeoutException",
        "InternalError",
        "InternalServerError",
        "InternalFailure",
        "ServiceUnavailable",
        "ServiceUnavailableError",
        "ServiceUnavailableException",
        "SlowDown",
        "RequestExpired",
        "EC2ThrottledException",
        "503",
        "500",
    }
)


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 32.0,
    jitter: bool = False,
    retryable: Callable[[BaseException], bool] | None = None,
    label: str = "",
    logger: logging.Logger | None = None,
) -> T:
    """Call *fn*, retrying while *retryable* says the failure is transient.

    Sleeps only *between* attempts: an exhausted call raises immediately
    instead of waiting out one final back-off it can never use. Back-off is
    ``min(max_delay, base_delay * 2**i)`` for the sleep before attempt ``i+1``,
    optionally with up to 50% added jitter to decorrelate concurrent retriers.

    Args:
        fn: Zero-argument callable to execute.
        attempts: Total number of attempts, including the first (so
            ``attempts=1`` disables retrying).
        base_delay: Initial delay in seconds, doubled each attempt.
        max_delay: Cap on any single sleep, applied after jitter.
        jitter: Add up to 50% random extra delay.
        retryable: Predicate deciding whether an exception is worth retrying.
            Defaults to retrying every ``Exception``.
        label: Human-readable operation name for log messages.
        logger: Logger for the retry warnings. Defaults to this module's, but
            callers pass their own so messages stay attributed to them.

    Returns:
        Whatever *fn* returns on its first successful attempt.

    Raises:
        The final exception, unwrapped, once attempts are exhausted or
        *retryable* rejects it.
    """
    log = logger if logger is not None else _logger
    is_retryable = retryable if retryable is not None else _always_retry
    name = label or getattr(fn, "__name__", repr(fn))

    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            is_last = attempt >= attempts - 1
            if is_last or not is_retryable(exc):
                raise

            delay = min(max_delay, base_delay * (2**attempt))
            if jitter:
                delay = min(max_delay, delay + random.uniform(0, delay * 0.5))
            log.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                name,
                attempt + 1,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)

    # Unreachable: the loop either returns or raises. Present so that static
    # analysis sees every path accounted for.
    raise AssertionError("retry_call exhausted its loop without returning or raising")


def _always_retry(_exc: BaseException) -> bool:
    return True


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------
def is_transient_aws_error(exc: BaseException) -> bool:
    """Is *exc* an AWS/network failure that a retry could plausibly fix?

    ``ClientError`` is judged by its error code against
    :data:`RETRYABLE_AWS_CODES`; transport-level failures are always transient.
    """
    from botocore.exceptions import ClientError, EndpointResolutionError

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in RETRYABLE_AWS_CODES
    return isinstance(exc, ConnectionError | OSError | EndpointResolutionError)


#: Substrings that mark a download failure as transient.
_TRANSIENT_DOWNLOAD_INDICATORS = (
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
)

#: Substrings that mark a download failure as permanent. Retrying these wastes
#: the full back-off budget on something that can never succeed.
_PERMANENT_DOWNLOAD_INDICATORS = (
    "video unavailable",
    "private video",
    "removed",
    "age-restricted",
    "geo-restricted",
    # yt-dlp's actual geo-block wording, which none of the previous markers
    # matched: "The uploader has not made this video available in your country".
    "available in your country",
    "not available",
    "copyright",
    "terminated",
    "sign in",
    "members-only",
    "members only",
    "drm",
)


def is_transient_download_error(exc: BaseException) -> bool:
    """Is *exc* a download failure worth retrying?

    Errors from yt-dlp arrive as opaque messages rather than typed exceptions,
    so this inspects the text. Transient markers win over permanent ones, and
    an unrecognised error is assumed transient: a needless retry costs seconds,
    while wrongly giving up loses an episode.
    """
    msg = str(exc).lower()
    if any(marker in msg for marker in _TRANSIENT_DOWNLOAD_INDICATORS):
        return True
    return not any(marker in msg for marker in _PERMANENT_DOWNLOAD_INDICATORS)
