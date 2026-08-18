"""Download and upload channel artwork as folder.jpg.

Some media players (including CloudBeats) display folder.jpg as album art
when browsing directories. This module downloads the YouTube thumbnail
and places it alongside the media files.
"""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)


def download_thumbnail(url: str, output_dir: str, filename: str = "folder.jpg") -> Path | None:
    """Download a thumbnail URL and save it locally.

    Args:
        url: Thumbnail image URL from YouTube metadata.
        output_dir: Directory to save the file.
        filename: Output filename (default: folder.jpg).

    Returns:
        Path to the downloaded file, or None on failure.
    """
    if not url:
        return None

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_path = Path(output_dir) / filename

    try:
        urllib.request.urlretrieve(url, str(output_path))
        logger.debug("Downloaded thumbnail: %s", output_path)
        return output_path
    except Exception as exc:
        logger.warning("Failed to download thumbnail (non-fatal): %s", exc)
        return None
