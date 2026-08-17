"""Embed metadata tags (title, artist) into audio/video files.

With --embed-metadata and --embed-thumbnail, yt-dlp handles the bulk of
tagging. This module serves as a best-effort fallback to ensure title and
artist are present even if yt-dlp'\''s embedding was partial (e.g. some
extractors skip artist).

Uses mutagen for m4a/mp4, best-effort (never fails the pipeline).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def tag_file(path: Path, title: str, artist: str) -> None:
    """Ensure title and artist metadata are present. Skips unsupported formats."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".m4v"):
            _tag_mp4(path, title, artist)
        elif suffix == ".mp3":
            _tag_mp3(path, title, artist)
        else:
            logger.debug("Skipping tagging for unsupported format: %s", suffix)
    except Exception as exc:
        logger.warning("Tagging failed for %s (non-fatal): %s", path.name, exc)


def _tag_mp4(path: Path, title: str, artist: str) -> None:
    from mutagen.mp4 import MP4

    audio = MP4(str(path))
    # Only fill in missing fields — don'\''t overwrite what yt-dlp embedded
    if not audio.tags.get("\xa9nam"):
        audio["\xa9nam"] = [title]
    if not audio.tags.get("\xa9ART"):
        audio["\xa9ART"] = [artist]
    audio.save()
    logger.debug("Tagged MP4 (fill-in): %s — %s", artist, title)


def _tag_mp3(path: Path, title: str, artist: str) -> None:
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3NoHeaderError

    try:
        tags = EasyID3(str(path))
    except ID3NoHeaderError:
        from mutagen.id3 import ID3

        ID3().save(str(path))
        tags = EasyID3(str(path))

    if not tags.get("title"):
        tags["title"] = title
    if not tags.get("artist"):
        tags["artist"] = artist
    tags.save()
    logger.debug("Tagged MP3 (fill-in): %s — %s", artist, title)