"""CLI entry point for MediaSync.

Usage:
    python -m mediasync            # run full pipeline
    python -m mediasync --dry-run  # show what would be processed
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from mediasync.config import Config
from mediasync.pipeline import RunStats, run


def main(argv: list[str] | None = None) -> int:
    """Entry point for MediaSync CLI."""
    parser = argparse.ArgumentParser(
        description="MediaSync — YouTube to pCloud, driven by Notion"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending entries without processing",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset all done/failed entries to pending and re-process everything",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Migrate existing files to channel-grouped folders and regenerate playlists",
    )
    parser.add_argument(
        "--regenerate-playlists",
        action="store_true",
        help="Regenerate All/Recent playlists from current Notion state (no downloads)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate storage connection and token health without processing",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = Config.from_env()
    except ValueError as exc:
        logging.error("Configuration error: %s", exc)
        return 1

    if args.migrate:
        return _run_migration(config, dry_run=args.dry_run)

    if args.regenerate_playlists:
        return _run_regenerate_playlists(config)

    if args.check:
        return _check_health(config)

    if args.dry_run:
        return _dry_run(config)

    if args.reset:
        _reset(config)

    start = time.time()
    stats = run(config)
    elapsed = int(time.time() - start)

    _notify(config, stats, elapsed)
    return 1 if stats.failed > 0 else 0


def _run_migration(config: Config, *, dry_run: bool = False) -> int:
    """Run migration to apply new features to existing data."""
    from mediasync.migrate import migrate

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\nMediaSync Migration ({mode})")
    print("=" * 40)
    print("This will:")
    print("  - Move files to channel-grouped folders")
    print("  - Upload folder.jpg artwork")
    print("  - Regenerate All/Recent playlists")
    print()

    stats = migrate(config, dry_run=dry_run)

    print("\nResults:")
    print(f"  Moved:     {stats['moved']}")
    print(f"  Skipped:   {stats['skipped']}")
    print(f"  Failed:    {stats['failed']}")
    print(f"  Playlists: {stats['playlists']}")

    return 1 if stats["failed"] > 0 else 0


def _run_regenerate_playlists(config: Config) -> int:
    """Regenerate standing playlists from current Notion state."""
    from mediasync.migrate import regenerate_playlists

    count = regenerate_playlists(config)
    print(f"Regenerated {count} playlists")
    return 0


def _check_health(config: Config) -> int:
    """Validate storage connection health."""

    print(f"Storage backend: {config.storage_backend}")

    if config.storage_backend == "onedrive":
        from mediasync.onedrive_client import OneDriveClient, OneDriveError
        try:
            client = OneDriveClient(
                config.onedrive_client_id,
                config.onedrive_client_secret,
                config.onedrive_refresh_token,
            )
        except OneDriveError as exc:
            print(f"FAIL: Token refresh failed — {exc}")
            print("The refresh token may have expired (90-day rolling window).")
            print("Re-run the OAuth flow to obtain a new token.")
            return 1

        if client.check_health():
            print("OK: OneDrive connection healthy")
            return 0
        else:
            print("FAIL: OneDrive health check failed")
            return 1
    elif config.storage_backend == "s3":
        from mediasync.s3_client import S3Client
        try:
            client = S3Client(config.s3_bucket, config.s3_region)
            # Verify bucket access with a HEAD request
            client._client.head_bucket(Bucket=config.s3_bucket)
            print(f"OK: S3 bucket '{config.s3_bucket}' accessible")
            return 0
        except Exception as exc:
            print(f"FAIL: S3 health check failed — {exc}")
            return 1
    else:
        print(f"Unknown storage backend: {config.storage_backend}")
        return 1


def _reset(config: Config) -> None:
    """Reset all done/failed entries to pending so they get re-processed."""
    from mediasync.notion_client import NotionClient

    notion = NotionClient(config.notion_token, config.notion_database_id)
    processed = notion.get_processed()

    if not processed:
        logging.info("Nothing to reset")
        return

    logging.info("Resetting %d entries to pending...", len(processed))
    for entry in processed:
        notion.reset_status(entry.page_id)
    logging.info("Reset complete")


def _dry_run(config: Config) -> int:
    """Print pending entries without processing."""
    from mediasync.notion_client import NotionClient

    notion = NotionClient(config.notion_token, config.notion_database_id)

    pending = notion.get_pending()
    deletions = notion.get_deletions()

    print(f"\nPending downloads: {len(pending)}")
    for entry in pending:
        print(f"  [{entry.profile}] {entry.format.value}: {entry.url}")

    print(f"\nPending deletions: {len(deletions)}")
    for entry in deletions:
        print(f"  [{entry.profile}] {entry.file_key}")

    return 0


def _notify(config: Config, stats: RunStats, elapsed_secs: int) -> None:
    """Send completion notification via Herald (if enabled)."""
    if not config.herald_enabled:
        return

    import shutil
    if not shutil.which("herald"):
        return

    mins, secs = divmod(elapsed_secs, 60)
    lines = [
        f"MediaSync — {mins}m {secs}s",
        f"  Processed: {stats.processed}",
        f"  Failed: {stats.failed}",
        f"  Deleted: {stats.deleted}",
        f"  Skipped: {stats.skipped}",
    ]
    message = "\n".join(lines)

    import subprocess
    cmd = ["herald", "notify", "--parse-mode", "plain", "--strict", "--message", message]
    job_id = config.herald_job_id
    if job_id:
        cmd += ["--job", job_id]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
