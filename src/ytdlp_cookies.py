"""Auto-inject cookies.txt into yt-dlp calls.

Discovers cookies.txt once, for both the library-based podcast pipeline and
the subprocess-based MediaSync downloader, and provides it to yt-dlp for both
extraction and download. Without cookies YouTube serves bot-detection
challenges instead of media.

The two code paths used to search different locations with different size
floors, so MediaSync silently ran cookie-less whenever it was invoked from
anywhere but the repository root. The search list below is the union of both,
ordered most- to least-specific.
"""

import logging
from pathlib import Path

import settings

logger = logging.getLogger(__name__)

#: A cookies.txt smaller than this cannot hold a usable session: the Netscape
#: header alone is ~50 bytes, and the authenticated cookies YouTube needs are
#: several hundred. Treating a tiny file as valid means silently downloading
#: without authentication, which surfaces later as bot detection.
MIN_COOKIE_BYTES = 100

#: Overrides discovery entirely when set to an existing file.
COOKIES_ENV = "YTDLP_COOKIES"


def _candidates() -> list[Path]:
    repo_root = Path(__file__).resolve().parent.parent
    return [
        repo_root / "cookies.txt",
        Path.cwd() / "cookies.txt",
        Path.home() / "PodcastDrive" / "cookies.txt",
        Path.home() / "cookies.txt",
        Path.home() / ".config" / "yt-dlp" / "cookies.txt",
    ]


def get_cookies_path() -> str | None:
    """Return the path to a usable cookies.txt, or None if there isn't one.

    ``YTDLP_COOKIES`` takes precedence when it points at an existing file.
    Otherwise the first candidate that exists and is at least
    :data:`MIN_COOKIE_BYTES` long wins.
    """
    override = settings.get(COOKIES_ENV)
    if override:
        path = Path(override)
        if path.is_file():
            return str(path)
        logger.warning("%s=%s does not exist — falling back to discovery", COOKIES_ENV, override)

    seen: set[Path] = set()
    for candidate in _candidates():
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if candidate.is_file() and candidate.stat().st_size >= MIN_COOKIE_BYTES:
                return str(candidate)
        except OSError:
            continue
    return None


def inject_cookies(ydl_opts: dict) -> dict:
    """Add ``cookiefile`` to yt-dlp options if cookies.txt exists."""
    if "cookiefile" not in ydl_opts:
        cookies = get_cookies_path()
        if cookies:
            ydl_opts["cookiefile"] = cookies
    return ydl_opts


def cookie_args() -> list[str]:
    """Return the ``--cookies`` CLI flags for subprocess yt-dlp calls.

    Empty when no usable cookies file was found, so callers can splice it
    unconditionally.
    """
    cookies = get_cookies_path()
    return ["--cookies", cookies] if cookies else []
