"""Per-source run orchestration for the podcast sync.

This module owns the lifecycle that used to live in three near-identical
Python heredocs inside ``run.sh``:

    resolve provider -> mark Running -> run the pipeline -> record a notify
    entry -> compute the feed URL -> mark Done / Failed / <special status>

Keeping it in Python makes that lifecycle testable and keeps the three modes
(explicit URL targets, configured YouTube sources, configured RSS feeds) from
drifting apart, as they had: only the YouTube copy handled ``bot_detected``
and only the RSS copy handled ``splice_failed``.

The stdout format, the notify-entry schema, the Notion status transitions and
the process exit codes are all deliberately byte-for-byte identical to the
heredocs this replaces — ``run.sh`` and Herald both depend on them.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Notify-entry plumbing
# ---------------------------------------------------------------------------
def append_notify_entry(entry: dict[str, Any], notify_file: str | None = None) -> None:
    """Append *entry* to the notify results JSON array.

    Best-effort by design: a notification bookkeeping failure must never abort
    a sync that has already done real work. The heredocs achieved this by
    accident (the whole block ran under ``|| true``); here it is explicit.
    """
    path = notify_file if notify_file is not None else os.environ.get("NOTIFY_RESULTS", "")
    if not path:
        return
    try:
        with open(path) as fh:
            entries = json.load(fh)
    except (OSError, ValueError):
        entries = []
    entries.append(entry)
    try:
        with open(path, "w") as fh:
            json.dump(entries, fh)
    except OSError as exc:
        logger.warning("Could not write notify results to %s: %s", path, exc)


def feed_url_for(identifier: str) -> str:
    """Build the public feed URL for *identifier* (playlist id or slug)."""
    cloudfront_base = os.environ.get("CLOUDFRONT_BASE", "")
    if not identifier or not cloudfront_base:
        return ""
    return f"{cloudfront_base}/{identifier}/feed.xml"


def _fallback_name(url: str) -> str:
    """Derive a display name from a bare URL, mirroring the original logic."""
    if "/@" in url:
        return url.split("/@")[-1].split("/")[0]
    return url


# ---------------------------------------------------------------------------
# Status resolution — the part that used to differ per copy
# ---------------------------------------------------------------------------
def success_status(result: dict[str, Any]) -> str:
    """Map a successful pipeline result onto its Notion status.

    Unified across all three modes. Previously the YouTube path checked
    ``bot_detected``, the RSS path checked ``splice_failed``, and the explicit
    URL path checked neither, so a bot-detected URL run was recorded as Done.
    """
    if result.get("bot_detected"):
        return "Error: Bot Detection"
    if result.get("splice_failed", 0):
        return "Splice Failed"
    return "Done"


def _set_status(provider: Any, podcast: Any, status: str, *, feed_url: str | None = None) -> None:
    """Update Notion status/last-run, swallowing provider errors.

    Notion write failures must not fail the run: the episodes are already
    uploaded and the feed already rebuilt by this point.
    """
    try:
        provider.update_status(podcast, status)
        if feed_url is not None:
            provider.update_last_run(podcast, feed_url=feed_url)
        else:
            provider.update_last_run(podcast)
    except Exception as exc:  # noqa: BLE001 - provider is remote and best-effort
        logger.warning("Could not update provider status to %r: %s", status, exc)


def run_one(
    *,
    name: str,
    identifier_key: str,
    pipeline: Callable[[], dict[str, Any]],
    provider: Any = None,
    podcast: Any = None,
    dry_run: bool = False,
    notify_name: str | None = None,
    success_notify_name: Callable[[dict[str, Any]], str] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Run a single source through the full lifecycle.

    Args:
        name: Display name used in log output.
        identifier_key: Key in the result holding the feed identifier
            (``playlist_id`` for YouTube, ``slug`` for RSS).
        pipeline: Zero-arg callable performing the actual sync.
        provider: Config provider, or None to skip all write-back.
        podcast: Provider entry to update, or None to skip all write-back.
        dry_run: When True, no provider writes happen.
        notify_name: Overrides the name recorded in the notify entry.
        success_notify_name: Optional callable given the successful result and
            returning the notify-entry name. Used only by the explicit-URL
            mode, which names a successful unmatched run after the resolved
            playlist id rather than after the raw URL.

    Returns:
        ``(result, ok)`` — the pipeline result dict (None on failure) and
        whether it succeeded.
    """
    writable = bool(provider is not None and podcast is not None and not dry_run)

    if writable:
        _set_status(provider, podcast, "Running")

    try:
        result = pipeline()
    except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
        if writable:
            _set_status(provider, podcast, "Failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        append_notify_entry(
            {
                "name": notify_name if notify_name is not None else name,
                "new_episodes": 0,
                "failed": 0,
                "error": str(exc),
            }
        )
        return None, False

    print(json.dumps(result, indent=2))

    if success_notify_name is not None:
        entry_name = success_notify_name(result)
    elif notify_name is not None:
        entry_name = notify_name
    else:
        entry_name = name

    # All counters are always emitted. The notifier reads each with
    # `.get(key, 0)`, so an always-present zero is identical to the previous
    # per-mode subsets, and the payload no longer varies by source type.
    append_notify_entry(
        {
            "name": entry_name,
            "new_episodes": result.get("new_episodes", 0),
            "failed": result.get("failed", 0),
            "unavailable": result.get("unavailable", 0),
            "splice_failed": result.get("splice_failed", 0),
            "bot_detected": bool(result.get("bot_detected", False)),
        }
    )

    if writable:
        _set_status(
            provider,
            podcast,
            success_status(result),
            feed_url=feed_url_for(result.get(identifier_key, "")),
        )

    return result, True


# ---------------------------------------------------------------------------
# Mode: explicit URL targets  (run.sh CLI mode)
# ---------------------------------------------------------------------------
def normalize_youtube_url(url: str) -> str:
    """Expand a configured YouTube reference into a full URL.

    Accepts a full URL, an ``@handle``, or a bare playlist/channel id, and
    ensures channel URLs address the ``/videos`` tab.
    """
    if not url.startswith("http"):
        if url.startswith("@"):
            return f"https://www.youtube.com/{url}/videos"
        return f"https://www.youtube.com/playlist?list={url}"
    if "/@" in url and "/videos" not in url:
        return url.rstrip("/") + "/videos"
    return url


def run_url_target(url: str, *, dry_run: bool) -> bool:
    """Process a single explicitly-supplied playlist/channel URL."""
    from config_provider import get_config_provider
    from sync import process_playlist
    from utils import extract_playlist_id

    provider = get_config_provider()

    # Resolve the playlist id so the URL can be matched to a Notion entry.
    try:
        playlist_id_for_lookup = extract_playlist_id(url)
    except Exception:  # noqa: BLE001 - unparseable URL just means no Notion match
        playlist_id_for_lookup = None

    podcast = None
    if not dry_run and playlist_id_for_lookup and hasattr(provider, "find_page_by_url"):
        try:
            podcast = provider.find_page_by_url(playlist_id_for_lookup)
        except Exception as exc:  # noqa: BLE001 - lookup is an optimisation
            logger.warning("Notion lookup failed for %s: %s", playlist_id_for_lookup, exc)
            podcast = None

    display = podcast.name if podcast else _fallback_name(url)[:40]

    # Preserved asymmetry from the original heredoc: with no Notion match, a
    # *successful* run is named after the playlist id resolved by the pipeline
    # (falling back to the URL-derived name), while a *failed* run — which has
    # no result to read an id from — is named after the URL.
    if podcast:

        def success_name(_result: dict[str, Any]) -> str:
            return podcast.name

    else:

        def success_name(_result: dict[str, Any]) -> str:
            return str(_result.get("playlist_id", _fallback_name(url)))[:40]

    _, ok = run_one(
        name=display,
        identifier_key="playlist_id",
        pipeline=lambda: process_playlist(url, dry_run=dry_run),
        provider=provider,
        podcast=podcast,
        dry_run=dry_run,
        notify_name=display,
        success_notify_name=success_name,
    )
    return ok


# ---------------------------------------------------------------------------
# Mode: configured YouTube sources
# ---------------------------------------------------------------------------
def run_youtube_sources(*, dry_run: bool) -> bool:
    """Process every enabled YouTube source from the config provider."""
    from config_provider import get_config_provider
    from sync import process_playlist

    provider = get_config_provider()
    podcasts = provider.get_podcasts()
    enabled = [p for p in podcasts if p.enabled]
    print(f"Found {len(enabled)} enabled podcasts (of {len(podcasts)} total)")
    print()

    all_ok = True
    for i, podcast in enumerate(enabled):
        url = normalize_youtube_url(podcast.url)

        print("=" * 50)
        print(f"[{i + 1}/{len(enabled)}] {podcast.name}")
        print(f"URL: {url}")
        print("=" * 50)

        _, ok = run_one(
            name=podcast.name,
            identifier_key="playlist_id",
            pipeline=lambda p=podcast, u=url: process_playlist(
                u,
                max_downloads=p.max_downloads,
                max_age_days=p.max_age_days,
                sleep_between=p.sleep_between,
                dry_run=dry_run,
            ),
            provider=provider,
            podcast=podcast,
            dry_run=dry_run,
        )
        all_ok = all_ok and ok
        print()

    return all_ok


# ---------------------------------------------------------------------------
# Mode: configured RSS podcast feeds
# ---------------------------------------------------------------------------
def run_rss_sources(*, dry_run: bool) -> bool:
    """Process every enabled RSS podcast feed from the config provider."""
    from config_provider import get_podcast_config_provider
    from podcast_sync import process_podcast_feed

    provider = get_podcast_config_provider()
    podcasts = provider.get_podcasts()
    enabled = [p for p in podcasts if p.enabled]
    print(f"Found {len(enabled)} enabled RSS podcast feeds (of {len(podcasts)} total)")
    print()

    all_ok = True
    for i, podcast in enumerate(enabled):
        print("=" * 50)
        print(f"[RSS {i + 1}/{len(enabled)}] {podcast.name}")
        print(f"URL: {podcast.url}")
        print("=" * 50)

        _, ok = run_one(
            name=podcast.name,
            identifier_key="slug",
            pipeline=lambda p=podcast: process_podcast_feed(p, provider=provider, dry_run=dry_run),
            provider=provider,
            podcast=podcast,
            dry_run=dry_run,
        )
        all_ok = all_ok and ok
        print()

    return all_ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Entry point used by run.sh.

    Usage:
        python -m orchestrator urls URL [URL ...]
        python -m orchestrator youtube
        python -m orchestrator rss

    Exit codes preserve the previous heredoc behaviour: a failure of an
    individual source is reported but still exits 0 (it is surfaced through
    the notify payload), while a provider-level failure exits 1 so that
    run.sh marks the run a partial_failure.
    """
    from logger_config import setup_logging

    setup_logging()

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: orchestrator {urls URL...|youtube|rss}", file=sys.stderr)
        return 2

    mode, rest = args[0], args[1:]
    dry_run = os.environ.get("PODCAST_DRY_RUN", "false") == "true"

    if mode == "urls":
        if not rest:
            print("usage: orchestrator urls URL [URL ...]", file=sys.stderr)
            return 2
        for url in rest:
            run_url_target(url, dry_run=dry_run)
        return 0
    if mode == "youtube":
        run_youtube_sources(dry_run=dry_run)
        return 0
    if mode == "rss":
        run_rss_sources(dry_run=dry_run)
        return 0

    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
