"""Runtime capability wiring for yt-dlp.

YouTube protects its media URLs with an obfuscated "n challenge" that has to be
solved by executing JavaScript before any audio/video format becomes reachable.
Recent yt-dlp releases no longer bundle the solver script: it is distributed as
an opt-in *remote component* (``ejs:github`` / ``ejs:npm``) that yt-dlp fetches
and caches on first use.

When the component is not allowed, yt-dlp silently degrades — it returns only
storyboard image formats, and every download then fails with
``Requested format is not available``.  Because that error looks like a missing
format rather than a broken runtime, it is easy to mistake for an unavailable
video, so the components are enabled by default here.
"""

import logging

import settings
from retry import looks_media_forbidden

logger = logging.getLogger(__name__)

#: Remote components allowed by default.  ``ejs:github`` is the distribution
#: recommended by yt-dlp; ``ejs:npm`` is the alternative mirror.
DEFAULT_REMOTE_COMPONENTS = "ejs:github"

#: Environment variable used to override the default.  Accepts a comma-separated
#: list of component names, or an empty string to disable fetching entirely.
REMOTE_COMPONENTS_ENV = "YTDLP_REMOTE_COMPONENTS"


def get_remote_components() -> list[str]:
    """Return the yt-dlp remote components that may be fetched.

    Reads ``YTDLP_REMOTE_COMPONENTS`` (comma-separated).  An unset variable
    falls back to :data:`DEFAULT_REMOTE_COMPONENTS`; an empty or whitespace-only
    value disables remote fetching altogether.

    Returns:
        List of component names, possibly empty.
    """
    raw = settings.get(REMOTE_COMPONENTS_ENV)
    return [part.strip() for part in raw.split(",") if part.strip()]


def inject_remote_components(ydl_opts: dict) -> dict:
    """Allow the JS challenge solver to be fetched, mutating *ydl_opts* in place.

    An explicit ``remote_components`` key already present in *ydl_opts* is left
    untouched so callers can opt out per invocation.

    Args:
        ydl_opts: yt-dlp options dict.

    Returns:
        The same dict, for convenient chaining.
    """
    if "remote_components" in ydl_opts:
        return ydl_opts

    components = get_remote_components()
    if components:
        ydl_opts["remote_components"] = components
    else:
        logger.warning(
            "yt-dlp remote components disabled via %s — YouTube n-challenge solving "
            "will fail and downloads may report 'Requested format is not available'",
            REMOTE_COMPONENTS_ENV,
        )
    return ydl_opts


def remote_component_args() -> list[str]:
    """Return the ``--remote-components`` CLI flags for subprocess yt-dlp calls.

    The library-based callers use :func:`inject_remote_components`; anything
    shelling out to the ``yt-dlp`` binary needs the equivalent as argv. Returns
    an empty list when remote fetching is disabled, so the caller can splice it
    unconditionally.
    """
    components = get_remote_components()
    if not components:
        logger.warning(
            "yt-dlp remote components disabled via %s — YouTube n-challenge solving "
            "will fail and downloads may report 'Requested format is not available'",
            REMOTE_COMPONENTS_ENV,
        )
        return []
    return ["--remote-components", ",".join(components)]


#: Environment variable listing the yt-dlp player clients to try after a 403.
#: Comma-separated and ordered; an empty value disables the fallback.
FALLBACK_CLIENTS_ENV = "YTDLP_FALLBACK_PLAYER_CLIENTS"


def fallback_player_clients() -> list[str]:
    """Return the yt-dlp player clients to try after a 403, in order.

    YouTube binds some clients' media URLs to a per-video GVS PO token. Without
    a token provider those URLs come back ``HTTP Error 403: Forbidden`` even
    though the video is fine, so both download paths re-extract with a client
    that needs no token. Shared here so the podcast pipeline and MediaSync
    honour the same knob.

    Returns:
        List of client names, possibly empty.
    """
    raw = settings.get(FALLBACK_CLIENTS_ENV)
    return [part.strip() for part in raw.split(",") if part.strip()]


def player_client_args(client: str) -> list[str]:
    """Return the ``--extractor-args`` CLI flags pinning yt-dlp to *client*.

    The library-based callers pass ``extractor_args`` in the options dict;
    anything shelling out to the ``yt-dlp`` binary needs the equivalent argv.
    """
    return ["--extractor-args", f"youtube:player_client={client}"]


#: Environment variable naming the player client to try *first*. YouTube has
#: finished binding the client yt-dlp picks by default to a per-video GVS PO
#: token, so leaving the choice to yt-dlp means paying a guaranteed 403 and a
#: re-extraction on every single download.
PRIMARY_CLIENT_ENV = "YTDLP_PLAYER_CLIENT"


def primary_player_client() -> str | None:
    """Return the player client to extract with first.

    Returns:
        The configured client name, or ``None`` to leave yt-dlp's own client
        choice alone (what an empty setting means).
    """
    return settings.get(PRIMARY_CLIENT_ENV).strip() or None


def player_client_chain() -> list[str | None]:
    """Return the player clients to try, in order, as ``None``-or-name entries.

    ``None`` means "let yt-dlp choose". It always appears in the chain, last
    unless it is the configured primary, so a client that becomes token-bound
    later still degrades to yt-dlp's own pick rather than failing the download.

    Returns:
        An ordered, duplicate-free list with at least one entry.
    """
    chain: list[str | None] = [primary_player_client()]
    for client in fallback_player_clients():
        if client not in chain:
            chain.append(client)
    if None not in chain:
        chain.append(None)
    return chain


def describe_client(client: str | None) -> str:
    """Name *client* for a log line or error message."""
    return client if client is not None else "yt-dlp default"


def describe_client_chain(chain: list[str | None]) -> str:
    """Name every client in *chain*, in order, for an error message."""
    return ", ".join(describe_client(client) for client in chain)


class YtdlpLogAdapter:
    """Routes yt-dlp's own output into a project logger.

    Without this, yt-dlp writes straight to stdout/stderr: the run log then
    carries bare ``ERROR: ...`` lines and Python tracebacks with no timestamp,
    module or video id, which reads as an unhandled crash even when the caller
    recovered. With a ``logger`` in the options dict yt-dlp routes everything
    through this class instead (``to_screen`` and ``write_debug`` to
    :meth:`debug`, ``report_warning`` to :meth:`warning`, ``report_error`` and
    any other stderr write to :meth:`error`).

    A media-URL 403 is demoted to debug: it is the expected first step of the
    player-client fallback, which logs its own warning and recovers, so copying
    it out at error level only trains the reader to ignore real errors.
    """

    def __init__(self, log: logging.Logger, context: str = "") -> None:
        self._log = log
        self._prefix = f"{context}: " if context else ""

    def _emit(self, level: int, message: str) -> None:
        self._log.log(level, "%syt-dlp: %s", self._prefix, message.strip())

    def debug(self, message: str) -> None:
        """Progress and verbose output: noise unless something went wrong."""
        self._emit(logging.DEBUG, message)

    def info(self, message: str) -> None:
        """Not part of yt-dlp's own protocol; accepted for safety."""
        self._emit(logging.DEBUG, message)

    def warning(self, message: str) -> None:
        self._emit(logging.WARNING, message)

    def error(self, message: str) -> None:
        if looks_media_forbidden(message):
            self._emit(logging.DEBUG, message)
            return
        self._emit(logging.ERROR, message)


def inject_logger(ydl_opts: dict, context: str = "", log: logging.Logger | None = None) -> dict:
    """Point *ydl_opts* at a :class:`YtdlpLogAdapter`, mutating it in place.

    An explicit ``logger`` already in *ydl_opts* is left untouched.

    Args:
        ydl_opts: yt-dlp options dict.
        context:  Identifier (usually a video id) prefixed to every line.
        log:      Logger to write through, so the line is attributed to the
                  calling module rather than to this one.

    Returns:
        The same dict, for convenient chaining.
    """
    if "logger" not in ydl_opts:
        ydl_opts["logger"] = YtdlpLogAdapter(log or logger, context)
    return ydl_opts
