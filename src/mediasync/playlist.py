"""M3U playlist file generation for MediaSync.

Generates extended M3U (.m3u8) playlists that CloudBeats and other players
can import. Paths are relative to the playlist file location so they work
regardless of how the cloud storage is mounted.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)


def generate_m3u(
    playlist_title: str,
    items: list[dict],
    output_dir: str,
) -> Path:
    """Generate an extended M3U8 playlist file.

    Args:
        playlist_title: Name for the playlist file (without extension).
        items: List of dicts with keys:
            - remote_key: str — the uploaded file path (e.g. "MediaSync/harshit/audio/Song.m4a")
            - title: str — display title
            - artist: str — artist/channel name
            - duration_secs: int — duration in seconds
        output_dir: Directory to write the .m3u8 file.

    Returns:
        Path to the generated .m3u8 file.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    safe_name = _sanitize_filename(playlist_title)
    playlist_path = Path(output_dir) / f"{safe_name}.m3u8"

    lines = ["#EXTM3U", f"#PLAYLIST:{playlist_title}"]

    for item in items:
        duration = item.get("duration_secs", -1)
        artist = item.get("artist", "Unknown")
        title = item.get("title", "Unknown")
        remote_key = item["remote_key"]

        # #EXTINF:duration,Artist - Title
        display = f"{artist} - {title}" if artist != "Unknown" else title
        lines.append(f"#EXTINF:{duration},{display}")
        lines.append(remote_key)

    content = "\n".join(lines) + "\n"
    playlist_path.write_text(content, encoding="utf-8")

    logger.info("Generated playlist: %s (%d items)", playlist_path.name, len(items))
    return playlist_path


def make_relative_keys(
    playlist_remote_folder: str,
    item_remote_keys: list[str],
) -> list[str]:
    """Convert absolute remote keys to paths relative to the playlist location.

    CloudBeats resolves relative paths from the playlist file'\''s directory.
    E.g. if playlist is at "MediaSync/harshit/playlists/MyList.m3u8"
    and a track is at "MediaSync/harshit/audio/Song.m4a",
    the relative path would be "../audio/Song.m4a".

    Args:
        playlist_remote_folder: The folder where the playlist file lives.
        item_remote_keys: List of absolute remote keys for the media files.

    Returns:
        List of relative path strings.
    """
    playlist_dir = PurePosixPath(playlist_remote_folder)
    relative = []
    for key in item_remote_keys:
        item_path = PurePosixPath(key)
        try:
            rel = _relative_posix(item_path, playlist_dir)
            relative.append(str(rel))
        except ValueError:
            # Fallback: use absolute path if no common ancestor
            relative.append(key)
    return relative


def _relative_posix(target: PurePosixPath, base: PurePosixPath) -> PurePosixPath:
    """Compute a relative path from base to target (POSIX)."""
    # Find common prefix
    target_parts = list(target.parts)
    base_parts = list(base.parts)

    common = 0
    # strict=False is intentional: the common prefix ends at the shorter path.
    for a, b in zip(target_parts, base_parts, strict=False):
        if a == b:
            common += 1
        else:
            break

    ups = len(base_parts) - common
    remainder = target_parts[common:]
    parts = [".."] * ups + remainder
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def _sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters from playlist name."""
    unsafe = '<>:"/\\|?*'
    result = name
    for ch in unsafe:
        result = result.replace(ch, "")
    result = " ".join(result.split())
    return result[:150] or "Playlist"
