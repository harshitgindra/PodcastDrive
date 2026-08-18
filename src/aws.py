"""Shared AWS session and client configuration.

The codebase creates AWS clients in 19 places with a bare ``boto3.client(...)``.
That has two costs. Every bare call builds its own ``boto3.Session``, so the
credential chain is resolved 19 times per run instead of once. And every client
gets botocore's stock defaults, in particular a 60-second *connect* timeout: on
a network blackhole a single S3 call stalls for a minute before the first retry,
and with retries layered on top a run can hang for many minutes with no log
output.

Rather than rewrite all 19 call sites (and the ~55 tests that patch them),
:func:`configure` installs one pre-configured session as boto3's default via
``set_default_client_config``. Existing bare ``boto3.client("s3")`` calls then
pick up the shared timeouts, retry mode and credentials with no change at all.
Entry points call :func:`configure` once at startup; it is idempotent.
"""

from __future__ import annotations

import logging
import os
import threading

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

#: Seconds to wait for a TCP connection. botocore defaults to 60, which turns a
#: blackholed network into a minute of silence per attempt. A connection that
#: has not been established in 10s is not going to be.
DEFAULT_CONNECT_TIMEOUT = 10.0

#: Seconds to wait for data on an established connection. Left at botocore's
#: default on purpose: Bedrock ad detection and Transcribe polling both run
#: close to it, so *raising* this is safe but lowering it would break them.
DEFAULT_READ_TIMEOUT = 60.0

#: Total attempts botocore makes internally, before any application-level
#: retry. ``standard`` mode classifies far more transient errors as retryable
#: than the ``legacy`` default while, unlike ``adaptive``, never introducing a
#: client-side token bucket that can block a caller.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_MODE = "standard"

_ENV_CONNECT_TIMEOUT = "AWS_CONNECT_TIMEOUT"
_ENV_READ_TIMEOUT = "AWS_READ_TIMEOUT"

_lock = threading.Lock()
_configured = False


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number — using %s", name, raw, default)
        return default


def default_config() -> Config:
    """Build the botocore config shared by every client in the process."""
    return Config(
        connect_timeout=_env_float(_ENV_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT),
        read_timeout=_env_float(_ENV_READ_TIMEOUT, DEFAULT_READ_TIMEOUT),
        retries={
            "max_attempts": DEFAULT_MAX_ATTEMPTS,
            "mode": DEFAULT_RETRY_MODE,
        },
    )


def configure(*, force: bool = False) -> boto3.Session:
    """Install a shared, timeout-configured session as boto3's default.

    Safe to call from any entry point, including more than once: the work
    happens on the first call unless *force* is set. Thread-safe, because the
    RSS pipeline configures AWS from the main thread while its
    ``ThreadPoolExecutor`` workers create clients.

    Returns:
        The shared session. Callers rarely need it; the point is the side
        effect on ``boto3.DEFAULT_SESSION``.
    """
    global _configured

    with _lock:
        if _configured and not force and boto3.DEFAULT_SESSION is not None:
            return boto3.DEFAULT_SESSION

        session = boto3.Session()
        session._session.set_default_client_config(default_config())
        boto3.DEFAULT_SESSION = session
        _configured = True

        logger.debug(
            "AWS clients configured: connect_timeout=%.1fs read_timeout=%.1fs "
            "retries=%s/%d attempts",
            _env_float(_ENV_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT),
            _env_float(_ENV_READ_TIMEOUT, DEFAULT_READ_TIMEOUT),
            DEFAULT_RETRY_MODE,
            DEFAULT_MAX_ATTEMPTS,
        )
        return session


def client(service: str, *, region: str | None = None, config: Config | None = None):
    """Create a client from the shared session.

    Preferred for new code over a bare ``boto3.client(...)``, since it cannot
    accidentally bypass the shared configuration.

    Args:
        service: AWS service name, e.g. ``"s3"``.
        region: Region override; defaults to the session's resolved region.
        config: Extra config merged over :func:`default_config`, for the rare
            call that needs its own timeout (a very large upload, say).
    """
    session = configure()
    merged = default_config().merge(config) if config is not None else None
    return session.client(service, region_name=region, config=merged)


def reset_for_testing() -> None:
    """Forget the shared session so a test can start from a clean slate."""
    global _configured
    with _lock:
        _configured = False
        boto3.DEFAULT_SESSION = None
