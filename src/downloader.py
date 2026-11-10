"""Audio download and MP3 conversion using yt_dlp with FFmpeg.

Downloads the best available audio from a YouTube video and converts it
to MP3 format. Processes one video at a time to keep disk usage low.

Transient download failures (network timeouts, rate limiting) are
retried automatically with exponential back-off via ``tenacity``.
"""

import contextlib
import glob
import logging
import os

import yt_dlp
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

import settings
from retry import is_media_forbidden_error, is_transient_download_error
from ytdlp_cookies import inject_cookies
from ytdlp_runtime import (
    describe_client,
    describe_client_chain,
    inject_logger,
    inject_remote_components,
    player_client_chain,
)

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Raised when a video download or conversion fails."""


class MediaForbiddenError(DownloadError):
    """Raised when YouTube refuses to serve the media URL itself (HTTP 403).

    Distinct from a generic :class:`DownloadError` because the cause is neither
    the video nor the network: the format URL the extractor handed back is bound
    to a GVS PO token that this deployment has no provider for, so googlevideo
    answers 403 for the actual download. Callers surface it as its own status so
    the run does not read as an ordinary, mysterious failure.
    """


def _ensure_ffmpeg_on_path() -> None:
    """Ensure FFmpeg is findable on PATH."""
    # Check common locations where FFmpeg might be installed
    for bin_dir in ["/opt/bin", "/usr/local/bin", "/opt/homebrew/bin"]:
        if os.path.isdir(bin_dir) and bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def _cleanup_intermediate_files(tmp_dir: str, video_id: str) -> None:
    """Remove any non-MP3 files matching *video_id* in *tmp_dir*."""
    pattern = os.path.join(tmp_dir, f"{video_id}.*")
    for path in glob.glob(pattern):
        if not path.endswith(".mp3"):
            try:
                os.remove(path)
                logger.debug("Cleaned up intermediate file: %s", path)
            except OSError:
                pass


#: Number of download attempts before giving up.
_MAX_RETRIES = settings.get("DOWNLOAD_MAX_RETRIES")

#: Initial wait in seconds between retries (doubles each attempt).
_RETRY_WAIT_MIN = settings.get("DOWNLOAD_RETRY_WAIT_MIN")
_RETRY_WAIT_MAX = settings.get("DOWNLOAD_RETRY_WAIT_MAX")


def _ydl_download(ydl_opts: dict, video_url: str, tmp_dir: str, video_id: str) -> None:
    """Perform the yt_dlp download, retrying on transient failures.

    Wrapped with tenacity so that network errors, rate-limit responses,
    and other transient exceptions are retried with exponential back-off.

    Only *transient* failures are retried. This used to retry every exception,
    so a permanently unavailable video (deleted, private, members-only,
    geo-blocked) still consumed all ``DOWNLOAD_MAX_RETRIES`` attempts and every
    back-off wait between them before failing with the same error it got the
    first time. The classification is shared with MediaSync via
    :func:`retry.is_transient_download_error`, so the two download paths cannot
    disagree about what is worth another attempt.

    Args:
        ydl_opts: yt_dlp options dict.
        video_url: YouTube video URL.
        tmp_dir: Temp directory (used for cleanup on failure).
        video_id: Video ID (used for cleanup on failure).

    Raises:
        DownloadError: After all retries are exhausted, or immediately for a
            failure that no retry could fix.
    """

    @retry(
        retry=retry_if_exception(is_transient_download_error),
        stop=stop_after_attempt(_MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=_RETRY_WAIT_MIN, max=_RETRY_WAIT_MAX),
        reraise=False,
    )
    def _attempt() -> None:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

    try:
        _attempt()
    except RetryError as exc:
        last = exc.last_attempt.exception()
        _cleanup_intermediate_files(tmp_dir, video_id)
        raise DownloadError(f"Failed to download/convert {video_id} after {_MAX_RETRIES} attempts: {last}") from last
    except Exception as exc:
        _cleanup_intermediate_files(tmp_dir, video_id)
        raise DownloadError(f"Failed to download/convert {video_id}: {exc}") from exc


def _discard(path: str) -> None:
    """Delete *path* if it exists, ignoring any error."""
    if os.path.exists(path):
        with contextlib.suppress(OSError):
            os.remove(path)


def _download_with_client_fallback(
    base_opts: dict,
    video_url: str,
    tmp_dir: str,
    video_id: str,
    mp3_path: str,
) -> None:
    """Download *video_url*, switching yt-dlp player client after a 403.

    YouTube binds googlevideo URLs to a GVS PO token per video for some
    clients. With no PO token provider installed the extractor still returns
    those formats, and a short range probe even succeeds, but the real download
    comes back as ``HTTP Error 403: Forbidden``. The video is fine and so is the
    network: only that one client URL is refused, and asking a different client
    for the same video yields a format that needs no token.

    The clients come from :func:`ytdlp_runtime.player_client_chain`, whose first
    entry is the configured primary (``YTDLP_PLAYER_CLIENT``). Every client
    yt-dlp picks by itself is now token-bound, so starting there used to cost a
    guaranteed 403 plus a re-extraction on every download; it is kept at the end
    of the chain instead.

    Retrying the identical request is useless, so the inner retry predicate
    treats a 403 as permanent and this loop re-extracts with each configured
    fallback client instead.

    Raises:
        MediaForbiddenError: Every client ended in a 403.
        DownloadError: Any other download failure, from the first client that
            hit it. A non-403 failure is not retried with another client.
    """
    last_exc: DownloadError | None = None
    chain = player_client_chain()

    for index, client in enumerate(chain):
        opts = dict(base_opts)
        if client is not None:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        if index:
            logger.warning(
                "%s: media URL refused (403) - retrying with yt-dlp player_client=%s",
                video_id,
                describe_client(client),
            )
        try:
            _ydl_download(opts, video_url, tmp_dir, video_id)
        except DownloadError as exc:
            _discard(mp3_path)
            last_exc = exc
            if not is_media_forbidden_error(exc):
                raise
            continue

        if index:
            logger.info("%s: downloaded using fallback player_client=%s", video_id, describe_client(client))
        return

    raise MediaForbiddenError(
        f"YouTube refused the media URL for {video_id} (HTTP 403) on every player "
        f"client tried ({describe_client_chain(chain)}): {last_exc}"
    ) from last_exc


def download_and_convert(
    video_url: str,
    video_id: str,
    tmp_dir: str,
) -> str:
    """Download audio from YouTube and convert to MP3.

    Transient failures are retried up to ``DOWNLOAD_MAX_RETRIES`` times
    (default: 3) with exponential back-off between attempts.

    Args:
        video_url: YouTube video URL.
        video_id: Video ID used as the output filename stem.
        tmp_dir: Working directory for temporary and output files.

    Returns:
        Absolute path to the resulting MP3 file.

    Raises:
        MediaForbiddenError: If every player client got HTTP 403 for the media
            URL: a PO-token-bound format, not a missing video.
        DownloadError: If the download or conversion fails for any other reason
            after all retries.
    """
    _ensure_ffmpeg_on_path()

    mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")
    output_template = os.path.join(tmp_dir, f"{video_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(settings.get("MP3_QUALITY")),
            }
        ],
    }
    inject_cookies(ydl_opts)
    inject_remote_components(ydl_opts)
    inject_logger(ydl_opts, video_id, logger)

    _download_with_client_fallback(ydl_opts, video_url, tmp_dir, video_id, mp3_path)

    # Clean up intermediate files (webm, opus, m4a, etc.)
    _cleanup_intermediate_files(tmp_dir, video_id)

    # Verify the MP3 exists and has content
    if not os.path.exists(mp3_path):
        raise DownloadError(f"MP3 file not found after conversion: {mp3_path}")

    if os.path.getsize(mp3_path) == 0:
        with contextlib.suppress(OSError):
            os.remove(mp3_path)
        raise DownloadError(f"MP3 file is empty after conversion: {mp3_path}")

    logger.info("Downloaded and converted %s → %s", video_id, mp3_path)
    return mp3_path
