"""Audio download and MP3 conversion using yt_dlp with FFmpeg.

Downloads the best available audio from a YouTube video and converts it
to MP3 format. Processes one video at a time to keep disk usage low.

Transient download failures (network timeouts, rate limiting) are
retried automatically with exponential back-off via ``tenacity``.
"""

import glob
import logging
import os
import sys

import yt_dlp
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Raised when a video download or conversion fails."""


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
_MAX_RETRIES = int(os.environ.get("DOWNLOAD_MAX_RETRIES", "3"))

#: Initial wait in seconds between retries (doubles each attempt).
_RETRY_WAIT_MIN = int(os.environ.get("DOWNLOAD_RETRY_WAIT_MIN", "5"))
_RETRY_WAIT_MAX = int(os.environ.get("DOWNLOAD_RETRY_WAIT_MAX", "60"))


def _ydl_download(ydl_opts: dict, video_url: str, tmp_dir: str, video_id: str) -> None:
    """Perform the yt_dlp download, retrying on transient failures.

    Wrapped with tenacity so that network errors, rate-limit responses,
    and other transient exceptions are retried with exponential back-off.

    Args:
        ydl_opts: yt_dlp options dict.
        video_url: YouTube video URL.
        tmp_dir: Temp directory (used for cleanup on failure).
        video_id: Video ID (used for cleanup on failure).

    Raises:
        DownloadError: After all retries are exhausted.
    """
    @retry(
        retry=retry_if_exception_type(Exception),
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
        raise DownloadError(
            f"Failed to download/convert {video_id} after {_MAX_RETRIES} attempts: {last}"
        ) from last
    except Exception as exc:
        _cleanup_intermediate_files(tmp_dir, video_id)
        raise DownloadError(
            f"Failed to download/convert {video_id}: {exc}"
        ) from exc


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
        DownloadError: If the download or conversion fails after all retries.
    """
    _ensure_ffmpeg_on_path()

    mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")
    output_template = os.path.join(tmp_dir, f"{video_id}.%(ext)s")

    ydl_opts = {
        "format": "18/bestaudio/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    try:
        _ydl_download(ydl_opts, video_url, tmp_dir, video_id)
    except DownloadError:
        # Clean up partial MP3 if it exists
        if os.path.exists(mp3_path):
            try:
                os.remove(mp3_path)
            except OSError:
                pass
        raise

    # Clean up intermediate files (webm, opus, m4a, etc.)
    _cleanup_intermediate_files(tmp_dir, video_id)

    # Verify the MP3 exists and has content
    if not os.path.exists(mp3_path):
        raise DownloadError(
            f"MP3 file not found after conversion: {mp3_path}"
        )

    if os.path.getsize(mp3_path) == 0:
        try:
            os.remove(mp3_path)
        except OSError:
            pass
        raise DownloadError(
            f"MP3 file is empty after conversion: {mp3_path}"
        )

    logger.info("Downloaded and converted %s → %s", video_id, mp3_path)
    return mp3_path
