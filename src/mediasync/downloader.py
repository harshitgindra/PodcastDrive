"""YouTube download for MediaSync — supports audio (m4a) and video (mp4).

Reuses the yt-dlp/ffmpeg infrastructure from PodcastDrive but with format
flexibility. Downloads are single-attempt by default (no retry needed for
on-demand personal use).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mediasync.notion_client import Format

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Raised when download or conversion fails."""


class DurationExceededError(DownloadError):
    """Raised when video exceeds the configured max duration."""


@dataclass
class DownloadResult:
    """Result of a successful download."""

    path: Path
    title: str
    artist: str
    duration_secs: int
    thumbnail_url: str
    format_type: str  # "audio" or "video"


def get_metadata(url: str) -> dict:
    """Fetch video metadata without downloading.

    Returns:
        Parsed JSON metadata dict from yt-dlp.

    Raises:
        DownloadError: If metadata extraction fails.
    """
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
        "--no-playlist",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise DownloadError(f"Metadata fetch failed: {result.stderr[:500]}")
    return json.loads(result.stdout)


def download(url: str, fmt: Format, *, output_dir: str, max_duration_secs: int) -> list[DownloadResult]:
    """Download media from YouTube.

    Args:
        url: YouTube video URL.
        fmt: Desired format (audio, video, or both).
        output_dir: Directory for downloaded files.
        max_duration_secs: Maximum allowed duration.

    Returns:
        List of DownloadResult (1 for audio/video, 2 for both).

    Raises:
        DurationExceededError: If video exceeds max duration.
        DownloadError: If download fails.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    meta = get_metadata(url)
    duration = int(meta.get("duration") or 0)
    if duration > max_duration_secs:
        raise DurationExceededError(
            f"Duration {duration}s exceeds limit of {max_duration_secs}s"
        )

    title = _sanitize_title(meta.get("title", "untitled"))
    artist = meta.get("uploader") or meta.get("channel") or "Unknown"
    thumbnail = meta.get("thumbnail", "")

    formats_to_download: list[str] = []
    if fmt in (Format.AUDIO, Format.BOTH):
        formats_to_download.append("audio")
    if fmt in (Format.VIDEO, Format.BOTH):
        formats_to_download.append("video")

    results: list[DownloadResult] = []
    for dl_format in formats_to_download:
        ext = "m4a" if dl_format == "audio" else "mp4"
        output_path = os.path.join(output_dir, f"{title}.{ext}")

        cmd = _build_cmd(url, dl_format, output_path)
        logger.info("Downloading %s as %s → %s", url, dl_format, output_path)

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            raise DownloadError(f"yt-dlp failed ({dl_format}): {proc.stderr[:500]}")

        actual_path = _find_output(Path(output_path))
        results.append(DownloadResult(
            path=actual_path,
            title=title,
            artist=artist,
            duration_secs=duration,
            thumbnail_url=thumbnail,
            format_type=dl_format,
        ))

    return results


def _build_cmd(url: str, fmt: str, output: str) -> list[str]:
    """Build yt-dlp command for the given format."""
    cmd = ["yt-dlp", "--no-playlist"]

    if fmt == "audio":
        cmd += [
            "-x",
            "--audio-format", "m4a",
            "--audio-quality", "0",
        ]
    else:
        cmd += [
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
        ]

    cmd += ["-o", output, url]
    return cmd


def _find_output(expected: Path) -> Path:
    """Find the actual output file (yt-dlp may adjust extension)."""
    if expected.exists():
        return expected
    # Search for alternatives with same stem
    for alt in expected.parent.glob(f"{expected.stem}.*"):
        if alt.suffix in (".m4a", ".mp3", ".opus", ".webm", ".mp4", ".mkv"):
            return alt
    raise DownloadError(f"Output file not found: {expected}")


def _sanitize_title(title: str) -> str:
    """Remove filesystem-unsafe characters from title."""
    unsafe = '<>:"/\\|?*'
    result = title
    for ch in unsafe:
        result = result.replace(ch, "")
    # Collapse whitespace and trim
    result = " ".join(result.split())
    return result[:200]
