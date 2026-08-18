"""Migration utilities for applying new features to existing data.

Handles:
1. Move files from flat folders to channel-grouped folders on OneDrive
2. Update Notion File Key with new paths
3. Upload folder.jpg artwork for existing channels
4. Regenerate standing playlists from current Notion state
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from mediasync.artwork import download_thumbnail
from mediasync.config import Config
from mediasync.downloader import DownloadError, get_metadata
from mediasync.notion_client import MediaEntry, NotionClient, Status
from mediasync.standing_playlists import generate_standing_playlists
from mediasync.storage import StorageBackend, create_storage

logger = logging.getLogger(__name__)


def migrate(config: Config, *, dry_run: bool = False) -> dict[str, int]:
    """Run migration on existing data.

    Steps:
        1. Fetch all done entries from Notion
        2. For each entry with a flat file path, look up channel from yt-dlp
        3. Move the file on storage to the channel-grouped path
        4. Update Notion File Key
        5. Upload folder.jpg for new channel folders
        6. Regenerate standing playlists

    Args:
        config: MediaSync configuration.
        dry_run: If True, log what would happen without making changes.

    Returns:
        Dict with counts: moved, skipped, failed, playlists.
    """
    stats = {"moved": 0, "skipped": 0, "failed": 0, "playlists": 0}

    notion = NotionClient(config.notion_token, config.notion_database_id)
    storage = None if dry_run else create_storage(config)

    for profile in config.profiles:
        done = notion.get_done_for_profile(profile.name)
        logger.info(
            "Profile %s: %d done entries to evaluate", profile.name, len(done)
        )

        artwork_uploaded: set[str] = set()

        for entry in done:
            if not entry.file_key:
                stats["skipped"] += 1
                continue

            try:
                result = _migrate_entry(
                    entry, config, notion, storage,
                    artwork_uploaded=artwork_uploaded,
                    dry_run=dry_run,
                )
                if result == "moved":
                    stats["moved"] += 1
                elif result == "skipped":
                    stats["skipped"] += 1
            except Exception as exc:
                logger.error("Failed to migrate %s: %s", entry.url, exc)
                stats["failed"] += 1

    # Regenerate standing playlists
    if not dry_run:
        stats["playlists"] = generate_standing_playlists(config, notion, storage)

    logger.info(
        "Migration complete: moved=%d, skipped=%d, failed=%d, playlists=%d",
        stats["moved"], stats["skipped"], stats["failed"], stats["playlists"],
    )
    return stats


def _migrate_entry(
    entry: MediaEntry,
    config: Config,
    notion: NotionClient,
    storage: StorageBackend | None,
    *,
    artwork_uploaded: set[str],
    dry_run: bool,
) -> str:
    """Migrate a single entry. Returns 'moved' or 'skipped'."""
    if not config.group_by_channel:
        return "skipped"

    keys = [k.strip() for k in entry.file_key.split(";") if k.strip()]
    new_keys: list[str] = []
    moved_any = False

    for key in keys:
        path = PurePosixPath(key)
        parts = path.parts

        # Determine if flat structure by counting path depth
        # Flat: prefix/profile/format/filename.ext
        # Grouped: prefix/profile/format/channel/filename.ext
        prefix_parts = PurePosixPath(config.prefix).parts
        relative_parts = parts[len(prefix_parts):]

        if len(relative_parts) <= 3:
            # Flat structure: needs migration
            channel = _get_channel_for_url(entry.url)
            if not channel:
                new_keys.append(key)
                continue

            profile_name = relative_parts[0]
            fmt_folder = relative_parts[1]
            filename = relative_parts[2]
            new_folder = f"{config.prefix}/{profile_name}/{fmt_folder}/{channel}"
            new_key = f"{new_folder}/{filename}"

            if dry_run:
                logger.info("Would move: %s -> %s", key, new_key)
                new_keys.append(new_key)
                moved_any = True
            else:
                success = _move_file(storage, key, new_key)
                if success:
                    new_keys.append(new_key)
                    moved_any = True

                    if new_folder not in artwork_uploaded:
                        _upload_artwork_for_entry(
                            entry.url, new_folder, storage, config.output_dir
                        )
                        artwork_uploaded.add(new_folder)
                else:
                    new_keys.append(key)
        else:
            new_keys.append(key)

    if moved_any and not dry_run:
        notion.update_status(
            entry.page_id, Status.DONE,
            file_key="; ".join(new_keys),
        )

    return "moved" if moved_any else "skipped"


def _get_channel_for_url(url: str) -> str | None:
    """Look up channel/uploader name from yt-dlp metadata."""
    try:
        meta = get_metadata(url)
        artist = meta.get("uploader") or meta.get("channel") or ""
        if artist:
            return _sanitize_channel(artist)
    except DownloadError:
        pass
    return None


def _sanitize_channel(name: str) -> str:
    """Clean channel name for use as folder name."""
    unsafe = '<>:"/\\|?*'
    result = name
    for ch in unsafe:
        result = result.replace(ch, "")
    result = " ".join(result.split()).strip(". ")
    return result[:100] or "Unknown"


def _move_file(storage: StorageBackend, old_key: str, new_key: str) -> bool:
    """Move a file on storage. Uses OneDrive PATCH move when available."""
    from mediasync.onedrive_client import OneDriveClient

    if isinstance(storage, OneDriveClient):
        return _onedrive_move(storage, old_key, new_key)

    logger.warning(
        "S3 migration requires re-download. Use --reset to re-process: %s", old_key
    )
    return False


def _onedrive_move(client, old_path: str, new_path: str) -> bool:
    """Move a file on OneDrive using the PATCH API."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    from mediasync.onedrive_client import GRAPH_API

    encoded_old = urllib.parse.quote(old_path)
    url = f"{GRAPH_API}/me/drive/root:/{encoded_old}"

    new_posix = PurePosixPath(new_path)
    new_parent = str(new_posix.parent)
    new_name = new_posix.name

    body = json.dumps({
        "parentReference": {"path": f"/drive/root:/{new_parent}"},
        "name": new_name,
    }).encode()

    req = urllib.request.Request(url, data=body, method="PATCH")
    req.add_header("Authorization", f"Bearer {client._access_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30):
            logger.info("Moved: %s -> %s", old_path, new_path)
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            client._access_token = client._refresh_access_token()
            return _onedrive_move(client, old_path, new_path)
        logger.error("Move failed for %s: HTTP %d", old_path, exc.code)
        return False
    except Exception as exc:
        logger.error("Move failed for %s: %s", old_path, exc)
        return False


def _upload_artwork_for_entry(
    url: str, remote_folder: str, storage: StorageBackend, output_dir: str
) -> None:
    """Download thumbnail and upload as folder.jpg."""
    try:
        meta = get_metadata(url)
        thumbnail = meta.get("thumbnail", "")
        if thumbnail:
            thumb_path = download_thumbnail(thumbnail, output_dir)
            if thumb_path:
                try:
                    storage.upload(thumb_path, remote_folder, "folder.jpg")
                finally:
                    thumb_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Artwork upload failed (non-fatal): %s", exc)


def regenerate_playlists(config: Config) -> int:
    """Regenerate standing playlists without any other processing.

    Useful when you want to update All/Recent playlists without
    downloading or moving anything.

    Returns:
        Number of playlists uploaded.
    """
    notion = NotionClient(config.notion_token, config.notion_database_id)
    storage = create_storage(config)
    return generate_standing_playlists(config, notion, storage)
