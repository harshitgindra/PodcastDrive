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
