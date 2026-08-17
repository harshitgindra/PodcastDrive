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

    if args.dry_run:
        return _dry_run(config)

    if args.reset:
        _reset(config)

    start = time.time()
    stats = run(config)
    elapsed = int(time.time() - start)

    _notify(config, stats, elapsed)
    return 1 if stats.failed > 0 else 0


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
