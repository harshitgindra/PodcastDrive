"""Embed metadata tags (title, artist) into audio/video files.

Uses mutagen for m4a/mp4, best-effort (never fails the pipeline).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def tag_file(path: Path, title: str, artist: str) -> None:
    """Embed title and artist metadata. Silently skips unsupported formats."""
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
    audio["\xa9nam"] = [title]
    audio["\xa9ART"] = [artist]
    audio.save()
    logger.debug("Tagged MP4: %s — %s", artist, title)


def _tag_mp3(path: Path, title: str, artist: str) -> None:
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3NoHeaderError

    try:
        tags = EasyID3(str(path))
    except ID3NoHeaderError:
        from mutagen.id3 import ID3

        ID3().save(str(path))
        tags = EasyID3(str(path))

    tags["title"] = title
    tags["artist"] = artist
    tags.save()
    logger.debug("Tagged MP3: %s — %s", artist, title)
