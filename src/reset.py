"""Reset orchestrator — wipes all S3 data for every enabled podcast.

Deletes all downloaded episodes (MP3s), feed.xml, and manifest.json for each
enabled podcast from both the YouTube and RSS Podcast config providers.

Intended to be called from ``run.sh --reset``.  Prompts for confirmation
unless ``--force`` is passed.

Usage (via run.sh):
    ./run.sh --reset            # prompts for confirmation
    ./run.sh --reset --force    # skips confirmation
"""

from __future__ import annotations

import logging
import os
import re
import sys

logger = logging.getLogger(__name__)


def _podcast_slug(name: str) -> str:
    """Convert a podcast name to a filesystem/S3-safe slug (mirrors podcast_sync.py)."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")[:60]
    return slug or "podcast"


def _collect_slugs() -> list[tuple[str, str]]:
    """Return a list of ``(display_name, s3_slug)`` tuples for all enabled podcasts.

    Gathers entries from both the YouTube config provider (Source=YouTube) and
    the RSS Podcast config provider (Source=Podcast).

    Returns:
        List of ``(name, slug)`` pairs; slug is the S3 key prefix for that podcast.
    """
    from config_provider import get_config_provider, get_podcast_config_provider
    from utils import extract_playlist_id

    slugs: list[tuple[str, str]] = []

    # --- YouTube playlists ---
    try:
        yt_provider = get_config_provider()
        yt_podcasts = yt_provider.get_podcasts()
        for podcast in yt_podcasts:
            if not podcast.enabled:
                continue
            try:
                slug = extract_playlist_id(podcast.url)
            except Exception:
                slug = podcast.url  # fallback: use raw URL as slug
            slugs.append((podcast.name, slug))
    except Exception as exc:
        logger.warning("[Reset] Could not load YouTube config: %s", exc)

    # --- RSS Podcast feeds ---
    try:
        rss_provider = get_podcast_config_provider()
        rss_podcasts = rss_provider.get_podcasts()
        for podcast in rss_podcasts:
            if not podcast.enabled:
                continue
            slug = _podcast_slug(podcast.name)
            slugs.append((podcast.name, slug))
    except Exception as exc:
        logger.warning("[Reset] Could not load RSS Podcast config: %s", exc)

    return slugs


def _count_episodes(s3_manager) -> int:
    """Return the number of existing episode MP3s in S3 for this podcast."""
    try:
        return len(s3_manager.list_existing_episodes())
    except Exception:
        return 0


def run_reset(force: bool = False) -> int:
    """Reset all enabled podcasts: delete episodes, feed.xml, and manifest.json.

    Args:
        force: When ``True``, skips the interactive confirmation prompt.

    Returns:
        Exit code — ``0`` on success, ``1`` if aborted or a fatal error occurs.
    """
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        print("ERROR: S3_BUCKET environment variable must be set.", file=sys.stderr)
        return 1

    # Collect all slugs to reset
    print("Collecting enabled podcasts…")
    slugs = _collect_slugs()

    if not slugs:
        print("No enabled podcasts found — nothing to reset.")
        return 0

    # Import S3Manager here to avoid import errors when running tests without AWS
    from s3_manager import S3Manager

    # Show what will be deleted
    print()
    print("The following podcasts will be fully reset (episodes + feed + manifest):")
    print()
    total_episodes = 0
    podcast_info: list[tuple[str, str, int]] = []
    for name, slug in slugs:
        s3 = S3Manager(bucket=bucket, playlist_id=slug)
        count = _count_episodes(s3)
        total_episodes += count
        podcast_info.append((name, slug, count))
        print(f"  • {name}  (slug={slug}, {count} episode(s) in S3)")

    print()
    print(
        f"⚠️  This will permanently delete {total_episodes} episode(s) across "
        f"{len(slugs)} podcast(s) from S3 bucket '{bucket}'."
    )
    print("   The directory structure will be recreated on the next run.")
    print()

    # Confirmation prompt (skipped with --force)
    if not force:
        try:
            answer = input("Are you sure you want to continue? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1

        if answer not in ("y", "yes"):
            print("Aborted — no changes made.")
            return 1

    # Perform the reset
    print()
    grand_total_episodes = 0
    for name, slug, _ in podcast_info:
        print(f"Resetting '{name}' (slug={slug})…")
        s3 = S3Manager(bucket=bucket, playlist_id=slug)
        try:
            result = s3.reset_podcast()
            grand_total_episodes += result["episodes_deleted"]
            feed_status = "✓" if result["feed_deleted"] else "–"
            manifest_status = "✓" if result["manifest_deleted"] else "–"
            print(
                f"  ✅  {result['episodes_deleted']} episode(s) deleted  "
                f"feed={feed_status}  manifest={manifest_status}"
            )
        except Exception as exc:
            print(f"  ❌  Failed to reset '{name}': {exc}", file=sys.stderr)
            logger.error("[Reset] Failed to reset '%s': %s", name, exc)

    print()
    print(
        f"✅  Reset complete — {grand_total_episodes} episode(s) deleted across "
        f"{len(slugs)} podcast(s)."
    )
    print("   Run './run.sh' to start fresh.")
    return 0


if __name__ == "__main__":
    import argparse

    from logger_config import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Reset all enabled podcasts: delete episodes, feed, and manifest from S3."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Skip the confirmation prompt.",
    )
    args = parser.parse_args()
    sys.exit(run_reset(force=args.force))
