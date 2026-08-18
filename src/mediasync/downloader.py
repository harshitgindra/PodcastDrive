"""YouTube download for MediaSync — supports audio (m4a) and video (mp4).

Reuses the yt-dlp/ffmpeg infrastructure from PodcastDrive but with format
flexibility. Downloads are single-attempt by default (no retry needed for
on-demand personal use).

Supports both single videos and playlists.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mediasync.notion_client import Format
from mediasync.retry import is_transient_download_error, retry_on_error

logger = logging.getLogger(__name__)

# Module-level metadata cache: avoids re-fetching metadata for items
# already checked during reconciliation. Keyed by normalized URL.
_metadata_cache: dict[str, dict] = {}


def cache_metadata(url: str, meta: dict) -> None:
    """Store metadata in the module-level cache."""
    _metadata_cache[url] = meta


def clear_metadata_cache() -> None:
    """Clear the metadata cache (for testing)."""
    _metadata_cache.clear()


class DownloadError(Exception):
    """Raised when download or conversion fails."""


class MissingDownloadError(DownloadError):
    """A file that was downloaded successfully is no longer on disk."""


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


def is_playlist(url: str) -> bool:
    """Detect if a URL points to a YouTube playlist."""
    return "list=" in url and "/watch?" not in url or "/playlist?" in url


def get_metadata(url: str) -> dict:
    """Fetch video metadata without downloading (single video only).

    Checks the module-level cache first; falls back to yt-dlp.

    Returns:
        Parsed JSON metadata dict from yt-dlp.

    Raises:
        DownloadError: If metadata extraction fails.
    """
    if url in _metadata_cache:
        return _metadata_cache[url]

    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
        "--no-playlist",
    ]
    cookies = _cookies_path()
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise DownloadError(f"Metadata fetch failed: {result.stderr[:500]}")
    meta = json.loads(result.stdout)
    _metadata_cache[url] = meta
    return meta


def get_playlist_metadata(url: str) -> list[dict]:
    """Fetch metadata for all items in a playlist.

    Returns:
        List of parsed JSON metadata dicts (one per video).

    Raises:
        DownloadError: If metadata extraction fails.
    """
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
        "--flat-playlist",
    ]
    cookies = _cookies_path()
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise DownloadError(f"Playlist metadata fetch failed: {result.stderr[:500]}")

    entries = []
    for line in result.stdout.strip().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    if not entries:
        raise DownloadError("Playlist is empty or unavailable")
    return entries




def get_full_playlist_metadata(url: str) -> list[dict]:
    """Fetch FULL metadata for all items in a playlist (single yt-dlp call).

    Unlike get_playlist_metadata (which uses --flat-playlist and only returns
    id/title), this resolves each video fully — returning uploader, duration,
    channel, thumbnail, etc. Slower (~30-60s for 100 items) but avoids
    per-item metadata fetches.

    Returns:
        List of full metadata dicts (one per video).

    Raises:
        DownloadError: If metadata extraction fails.
    """
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
    ]
    cookies = _cookies_path()
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)
    # Full resolution is slower; allow up to 10 minutes for large playlists
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise DownloadError(f"Full playlist metadata fetch failed: {result.stderr[:500]}")

    entries = []
    for line in result.stdout.strip().splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not entries:
        raise DownloadError("Playlist is empty or unavailable")
    return entries

def download(
    url: str,
    fmt: Format,
    *,
    output_dir: str,
    max_duration_secs: int,
    max_retries: int = 3,
) -> list[DownloadResult]:
    """Download media from YouTube (single video or playlist).

    Args:
        url: YouTube video or playlist URL.
        fmt: Desired format (audio, video, or both).
        output_dir: Directory for downloaded files.
        max_duration_secs: Maximum allowed duration per video.
        max_retries: Number of retry attempts for transient failures.

    Returns:
        List of DownloadResult — one per format per video.

    Raises:
        DurationExceededError: If any video exceeds max duration.
        DownloadError: If download fails.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if is_playlist(url):
        results = _download_playlist(url, fmt, output_dir=output_dir, max_duration_secs=max_duration_secs, max_retries=max_retries)
    else:
        results = _download_single(url, fmt, output_dir=output_dir, max_duration_secs=max_duration_secs, max_retries=max_retries)
    _assert_results_present(results)
    return results


def _assert_results_present(results: list[DownloadResult]) -> None:
    """Fail fast if a result points at a file that is no longer on disk.

    Without this the caller only discovers the loss at the tag/upload step,
    surfacing as a bare ``FileNotFoundError`` with no indication of which
    item vanished or why — which is exactly how the sibling-deletion bug in
    ``_discard_partials`` stayed unexplained.
    """
    missing = [r for r in results if not r.path.exists()]
    if missing:
        detail = ", ".join(f"{r.title!r} -> {r.path}" for r in missing)
        raise MissingDownloadError(
            f"{len(missing)} of {len(results)} downloaded file(s) disappeared before upload: {detail}"
        )


def cleanup_results(results: list[DownloadResult]) -> None:
    """Delete the local files behind *results*, ignoring anything already gone."""
    for result in results:
        try:
            result.path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not delete %s: %s", result.path, exc)


#: Extensions yt-dlp may leave behind for a given output stem: final media
#: containers, per-format streams, in-progress temp files, and thumbnails.
_LEFTOVER_EXTS = frozenset(
    {
        # media containers
        "m4a", "mp3", "opus", "webm", "mp4", "mkv", "aac", "ogg", "flac", "wav",
        # in-progress / auxiliary
        "part", "ytdl", "temp", "jpg", "jpeg", "png", "webp",
    }
)

#: Per-format stream suffix yt-dlp injects, e.g. ``Title.f140.m4a``.
_FORMAT_ID_RE = re.compile(r"^f\d+$")


def _is_leftover_for_stem(name: str, stem: str) -> bool:
    """Is *name* a file yt-dlp would have written for exactly *stem*?

    The match is anchored on ``{stem}.`` and every remaining dot-separated
    token must be a known extension or a yt-dlp format id.  A bare prefix
    test is *not* safe here: ``_claim_stem`` hands sibling playlist items
    stems like ``Song`` and ``Song (2)``, and a title may legitimately be a
    prefix of another (``Intro`` / ``Intro Part 1``).  Globbing ``Song*``
    therefore deleted a *different*, already-completed item mid-run, whose
    DownloadResult then failed with ENOENT at the tag/upload step.
    """
    prefix = f"{stem}."
    if not name.startswith(prefix):
        return False
    remainder = name[len(prefix):]
    if not remainder:
        return False
    tokens = remainder.split(".")
    return all(tok.lower() in _LEFTOVER_EXTS or _FORMAT_ID_RE.match(tok) for tok in tokens)


def _discard_partials(output_dir: str, stem: str) -> None:
    """Remove any (possibly partial) files yt-dlp wrote for *stem*.

    A failed or interrupted yt-dlp run leaves ``Title.m4a``, ``Title.part``,
    ``Title.f140.m4a`` and friends behind.  Nothing else ever cleans them up,
    so the temp directory grew without bound on every download failure.

    Only files belonging to *stem* itself are removed — never those of a
    sibling item whose title merely starts with the same text.
    """
    directory = Path(output_dir)
    if not directory.is_dir():
        return
    for leftover in directory.iterdir():
        if not leftover.is_file() or not _is_leftover_for_stem(leftover.name, stem):
            continue
        try:
            leftover.unlink()
            logger.info("Removed partial download %s", leftover)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not remove partial download %s: %s", leftover, exc)


def _claim_stem(title: str, claimed: set[str]) -> str:
    """Return a filename stem for *title* that is unique within *claimed*.

    Playlist items frequently share a title ("Intro", "Part 1", or the same
    track uploaded twice).  Both used to be written to ``{title}.{ext}``, so the
    second download silently overwrote the first and only one file was uploaded.
    """
    stem = title
    suffix = 1
    while stem.casefold() in claimed:
        suffix += 1
        stem = f"{title} ({suffix})"
    claimed.add(stem.casefold())
    return stem


def _download_single(
    url: str,
    fmt: Format,
    *,
    output_dir: str,
    max_duration_secs: int,
    max_retries: int = 3,
    claimed_stems: set[str] | None = None,
) -> list[DownloadResult]:
    """Download a single video."""
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

    # One stem per video, shared by its audio and video outputs, so that two
    # playlist items with the same title cannot overwrite each other.
    stem = _claim_stem(title, claimed_stems) if claimed_stems is not None else title

    results: list[DownloadResult] = []
    try:
        for dl_format in formats_to_download:
            ext = "m4a" if dl_format == "audio" else "mp4"
            output_path = os.path.join(output_dir, f"{stem}.{ext}")

            cmd = _build_cmd(url, dl_format, output_path)
            logger.info("Downloading %s as %s \u2192 %s", url, dl_format, output_path)

            attempt_count = [0]

            def _run_download() -> subprocess.CompletedProcess:
                # Clean up partials from a previous failed attempt (not first try)
                if attempt_count[0] > 0:
                    _discard_partials(output_dir, stem)
                attempt_count[0] += 1
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                if proc.returncode != 0:
                    raise DownloadError(f"yt-dlp failed ({dl_format}): {proc.stderr[:500]}")
                return proc

            retry_on_error(
                _run_download,
                max_retries=max_retries,
                retryable=is_transient_download_error,
                description=f"download {dl_format} for {url}",
            )

            actual_path = _find_output(Path(output_path))
            results.append(DownloadResult(
                path=actual_path,
                title=title,
                artist=artist,
                duration_secs=duration,
                thumbnail_url=thumbnail,
                format_type=dl_format,
            ))
    except BaseException:
        # Never leave half-written media behind: the caller only cleans up
        # results it was handed, and on failure it is handed nothing.
        cleanup_results(results)
        _discard_partials(output_dir, stem)
        raise

    return results


def _download_playlist(url: str, fmt: Format, *, output_dir: str, max_duration_secs: int, max_retries: int = 3) -> list[DownloadResult]:
    """Download all videos in a playlist."""
    playlist_meta = get_playlist_metadata(url)
    logger.info("Playlist has %d items", len(playlist_meta))

    results: list[DownloadResult] = []
    claimed_stems: set[str] = set()
    for idx, entry in enumerate(playlist_meta, 1):
        video_url = entry.get("url") or entry.get("webpage_url", "")
        if not video_url:
            # flat-playlist gives id; construct URL
            video_id = entry.get("id", "")
            if not video_id:
                logger.warning("Skipping playlist item %d: no URL or ID", idx)
                continue
            video_url = f"https://www.youtube.com/watch?v={video_id}"

        title = entry.get("title", f"track_{idx:03d}")
        logger.info("Playlist item %d/%d: %s", idx, len(playlist_meta), title)

        try:
            item_results = _download_single(
                video_url,
                fmt,
                output_dir=output_dir,
                max_duration_secs=max_duration_secs,
                max_retries=max_retries,
                claimed_stems=claimed_stems,
            )
            results.extend(item_results)
        except DurationExceededError as exc:
            logger.warning("Skipping playlist item %d (too long): %s", idx, exc)
            continue
        except DownloadError as exc:
            logger.error("Failed playlist item %d: %s", idx, exc)
            # The caller never sees `results`, so clean up what we already have.
            cleanup_results(results)
            raise

    if not results:
        raise DownloadError("No items could be downloaded from playlist")

    return results


def _cookies_path() -> Path | None:
    """Find cookies.txt in project root or home directory."""
    candidates = [
        Path.cwd() / "cookies.txt",
        Path.home() / "cookies.txt",
        Path.home() / ".config" / "yt-dlp" / "cookies.txt",
    ]
    for p in candidates:
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _build_cmd(url: str, fmt: str, output: str) -> list[str]:
    """Build yt-dlp command for the given format."""
    cmd = ["yt-dlp", "--no-playlist"]

    cookies = _cookies_path()
    if cookies:
        cmd += ["--cookies", str(cookies)]

    # Embed YouTube thumbnail as cover art and metadata (title, artist, etc.)
    cmd += ["--embed-thumbnail", "--embed-metadata"]

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
    """Find the actual output file (yt-dlp may adjust extension).

    Uses an explicit prefix comparison rather than ``glob`` so that titles
    containing glob metacharacters (``Song [live]``) still match their own
    file instead of being read as a character class and reported missing.
    """
    if expected.exists():
        return expected
    _MEDIA = (".m4a", ".mp3", ".opus", ".webm", ".mp4", ".mkv")
    prefix = f"{expected.stem}."
    fallback: Path | None = None
    for alt in sorted(expected.parent.iterdir()):
        if not alt.is_file() or not alt.name.startswith(prefix) or alt.suffix not in _MEDIA:
            continue
        # `Title.m4a` wins over a per-format leftover like `Title.f140.m4a`.
        if alt.stem == expected.stem:
            return alt
        if fallback is None:
            fallback = alt
    if fallback is not None:
        return fallback
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
