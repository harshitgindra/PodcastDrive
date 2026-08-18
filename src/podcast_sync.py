"""RSS podcast feed sync pipeline.

Orchestrates the full pipeline for ``Source=Podcast`` Notion entries:
  1. Resolve Apple Podcasts / iTunes URLs → real RSS feed URL (write-back).
  2. Fetch and parse the RSS feed.
  3. Diff episodes against existing S3 objects.
  4. Download new episodes, remove ads, upload to S3 (parallel, see PODCAST_EPISODE_WORKERS).
  5. Generate and upload ``feed.xml``.
  6. Update Notion status / last-run timestamp.

Environment variables:
    PODCAST_EPISODE_WORKERS – Number of episodes to process in parallel (default: 1 —
                              sequential, original behaviour).  Set to 3 for typical use;
                              download + Transcribe + Bedrock are all IO-bound so workers
                              spend most of their time waiting, not competing for CPU.
                              Higher values increase S3/Transcribe concurrency.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from email.utils import format_datetime
from xml.dom.minidom import parseString

import settings
from ad_remover import REMOVE_ADS_ERROR_CODES, remove_ads, validate_audio_file
from config_provider import PodcastConfig
from podcast_downloader import (
    EpisodeMeta,
    download_episode,
    episode_id_from_guid,
    fetch_feed_xml,
    is_apple_podcasts_url,
    parse_channel_thumbnail,
    parse_episodes,
    resolve_feed_url,
    search_feed_url_by_name,
)
from rss_generator import xml_safe
from s3_manager import S3Manager

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# iTunes namespace
_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

try:
    import xml.etree.ElementTree as ET

    ET.register_namespace("itunes", _ITUNES_NS)
except Exception:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _podcast_slug(name: str) -> str:
    """Convert a podcast name to a filesystem/S3-safe slug.

    Args:
        name: Human-readable podcast name.

    Returns:
        Lowercased, hyphen-separated slug, e.g. ``"my-podcast"``.
    """
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")[:60]
    return slug or "podcast"


def _format_duration(seconds: int) -> str:
    """Format seconds as ``H:MM:SS`` or ``M:SS``."""
    if seconds <= 0:
        return "0:00"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# CDN/SSAI detection — regex table matched against the episode download URL.
# Order matters: first match wins. SSAI (server-side ad-insertion) CDNs
# dynamically stitch per-request audio, which occasionally produces a
# corrupt/truncated fetch that crashes ffmpeg during splicing (confirmed for
# Megaphone and Acast in production). Known-flaky CDNs get extra splice
# retry attempts by default; unknown/direct-hosted sources use the plain
# SPLICE_MAX_ATTEMPTS_PER_RUN default.
_CDN_PATTERNS: list[tuple[str, str]] = [
    (r"megaphone\.fm|podtrac\.com", "megaphone"),
    (r"acast\.com", "acast"),
    (r"art19\.com", "art19"),
    (r"anchor\.fm|simplecastaudio\.com|simplecast\.com", "anchor"),
    (r"cloudfront\.net", "cloudfront"),
    (r"libsyn\.com", "libsyn"),
]

# Per-CDN splice-attempt overrides (fresh-download retries within a single run).
# Falls back to SPLICE_MAX_ATTEMPTS_PER_RUN (env, default 2) when the CDN is
# unknown or not listed here.
CDN_RETRY_OVERRIDES: dict[str, int] = {
    "megaphone": 3,
    "acast": 3,
}


def detect_cdn(url: str) -> str:
    """Best-effort CDN/SSAI provider tag derived from an episode download URL.

    Args:
        url: Episode enclosure URL (may be a redirect-tracking URL that
             embeds the true CDN host, e.g. ``podtrac.com/.../megaphone.fm/...``).

    Returns:
        A short lowercase tag (e.g. ``"megaphone"``, ``"acast"``) or
        ``"unknown"`` if no pattern matches.
    """
    if not url:
        return "unknown"
    for pattern, tag in _CDN_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return tag
    return "unknown"


def _splice_attempts_for_cdn(cdn: str) -> int:
    """Resolve the max fresh-download splice-retry attempts for a CDN tag."""
    if cdn in CDN_RETRY_OVERRIDES:
        return CDN_RETRY_OVERRIDES[cdn]
    return settings.get("SPLICE_MAX_ATTEMPTS_PER_RUN")


def _build_podcast_feed_xml(
    podcast: PodcastConfig,
    episodes: list[EpisodeMeta],
    episode_ids: list[str],
    cloudfront_base: str,
    slug: str,
    ep_sizes: dict[str, int] | None = None,
    channel_thumbnail: str = "",
    language: str = "en",
    manifest: dict | None = None,
) -> str:
    """Generate a minimal RSS 2.0 feed for *podcast* from cleaned episode list.

    Args:
        podcast:           Podcast configuration (name, url).
        episodes:          EpisodeMeta objects for episodes in the feed.
        episode_ids:       Parallel list of stable S3 episode IDs.
        cloudfront_base:   CloudFront distribution base URL (no trailing slash).
        slug:              S3 folder slug for this podcast.
        ep_sizes:          Optional dict mapping episode_id → file size in bytes
                           for accurate ``<enclosure length>`` values.
        channel_thumbnail: Artwork URL for the channel-level ``<itunes:image>``
                           and RSS ``<image>`` elements.  Falls back to the first
                           episode's thumbnail when empty.
        manifest:          Optional episode manifest dict for summary lookup.

    Returns:
        Pretty-printed RSS XML string.
    """
    if manifest is None:
        manifest = {}
    import xml.etree.ElementTree as ET

    if ep_sizes is None:
        ep_sizes = {}

    # Resolve best artwork: explicit channel thumbnail → first episode thumbnail
    artwork_url = channel_thumbnail or (episodes[0].thumbnail if episodes else "")

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    suffix = settings.get("FEED_TITLE_SUFFIX")
    ET.SubElement(channel, "title").text = xml_safe(podcast.name + suffix)
    ET.SubElement(channel, "link").text = xml_safe(podcast.url)
    ET.SubElement(channel, "description").text = xml_safe(podcast.description or podcast.name)
    ET.SubElement(channel, f"{{{_ITUNES_NS}}}summary").text = xml_safe(podcast.description or podcast.name)
    ET.SubElement(channel, "language").text = xml_safe(language)
    ET.SubElement(channel, "generator").text = "PodcastDrive"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(UTC))

    # Standard RSS 2.0 <image> block (required by many non-iTunes podcast apps)
    if artwork_url:
        rss_image = ET.SubElement(channel, "image")
        ET.SubElement(rss_image, "url").text = xml_safe(artwork_url)
        ET.SubElement(rss_image, "title").text = xml_safe(podcast.name + suffix)
        ET.SubElement(rss_image, "link").text = xml_safe(podcast.url)

    ET.SubElement(channel, f"{{{_ITUNES_NS}}}author").text = xml_safe(podcast.name)
    ET.SubElement(channel, f"{{{_ITUNES_NS}}}explicit").text = "no"

    subtitle = settings.get("FEED_SUBTITLE")
    if subtitle:
        ET.SubElement(channel, f"{{{_ITUNES_NS}}}subtitle").text = xml_safe(subtitle)

    # iTunes channel artwork
    if artwork_url:
        img_el = ET.SubElement(channel, f"{{{_ITUNES_NS}}}image")
        img_el.set("href", xml_safe(artwork_url))

    ep_ad_suffix = settings.get("EPISODE_AD_REMOVED_SUFFIX")

    for ep, ep_id in zip(episodes, episode_ids, strict=True):
        item = ET.SubElement(channel, "item")
        title = ep.title
        if ep_ad_suffix and manifest.get(ep_id, {}).get("ads_removed"):
            title += ep_ad_suffix
        ET.SubElement(item, "title").text = xml_safe(title)

        guid_el = ET.SubElement(item, "guid")
        guid_el.set("isPermaLink", "false")
        guid_el.text = xml_safe(ep.guid)

        cf_url = f"{cloudfront_base}/{slug}/episodes/{ep_id}.mp3"
        enc = ET.SubElement(item, "enclosure")
        enc.set("url", cf_url)
        enc.set("length", str(ep_sizes.get(ep_id, 0)))
        enc.set("type", "audio/mpeg")

        ET.SubElement(item, "pubDate").text = format_datetime(ep.pub_date)
        ET.SubElement(item, f"{{{_ITUNES_NS}}}duration").text = _format_duration(ep.duration)
        ET.SubElement(item, f"{{{_ITUNES_NS}}}explicit").text = "no"

        # Episode description: prefer AI summary from manifest
        desc = manifest.get(ep_id, {}).get("summary") or ep.title
        ET.SubElement(item, "description").text = xml_safe(desc)

        # Per-episode artwork (falls back to channel artwork if episode has none)
        ep_thumbnail = ep.thumbnail or artwork_url
        if ep_thumbnail:
            ep_img = ET.SubElement(item, f"{{{_ITUNES_NS}}}image")
            ep_img.set("href", xml_safe(ep_thumbnail))

    rough = ET.tostring(rss, encoding="unicode", xml_declaration=False)
    dom = parseString(rough)
    pretty = dom.toprettyxml(indent="  ", encoding=None)
    lines = pretty.split("\n")
    if lines and lines[0].startswith("<?xml"):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def process_podcast_feed(
    podcast: PodcastConfig,
    provider=None,
    dry_run: bool = False,
) -> dict:
    """Process a single RSS podcast: download new episodes, remove ads, upload, generate feed.

    Steps:
      1. If the URL is an Apple Podcasts link, resolve it to the real RSS feed URL
         and write it back to Notion (via *provider*) so subsequent runs skip lookup.
      2. Fetch and parse the RSS feed, filtering by ``max_age_days``.
      3. Diff against existing S3 episodes (by episode ID).
      4. For each new episode: download MP3 → remove ads → upload to S3.
      5. Rebuild and upload ``feed.xml``.
      6. Update Notion status / last-run.

    Args:
        podcast:  Podcast configuration (source must be ``"Podcast"``).
        provider: Config provider instance; used for Notion write-back.
                  Must expose ``update_url(podcast, url)`` if URL resolution
                  is needed.  ``None`` disables all write-back.
        dry_run:  When ``True``, perform all read-only steps and log what
                  *would* happen but skip downloads, S3 writes, and Notion updates.

    Returns:
        dict with keys: ``slug``, ``new_episodes``, ``skipped``, ``failed``.
    """
    bucket = settings.get("S3_BUCKET")
    if not bucket:
        raise ValueError("S3_BUCKET environment variable must be set")
    cloudfront_base = settings.get("CLOUDFRONT_BASE")
    if not cloudfront_base:
        raise ValueError("CLOUDFRONT_BASE environment variable must be set")

    max_age_days = podcast.max_age_days
    if max_age_days is None:
        max_age_days = settings.get("MAX_AGE_DAYS", default=0) or None

    max_episodes = podcast.max_downloads
    if max_episodes is None:
        max_episodes = settings.get("PODCAST_MAX_EPISODES")

    slug = _podcast_slug(podcast.name)
    tmp_dir = tempfile.mkdtemp(prefix=f"podcast-{slug}-")
    _run_start = time.monotonic()

    logger.info(
        "[PodcastSync] Starting '%s' (slug=%s, max_episodes=%s, max_age_days=%s, dry_run=%s)",
        podcast.name,
        slug,
        max_episodes,
        max_age_days,
        dry_run,
    )

    try:
        # ------------------------------------------------------------------
        # Step 1: Resolve feed URL
        #   1a. No URL → search iTunes by podcast name
        #   1b. Apple Podcasts / iTunes URL → resolve to real RSS feed via lookup
        # ------------------------------------------------------------------
        feed_url = podcast.url

        if not feed_url:
            # No URL configured — search iTunes by podcast name
            logger.info("[PodcastSync] No URL for '%s' — searching iTunes by name", podcast.name)
            discovered = search_feed_url_by_name(podcast.name)
            if not discovered:
                logger.error(
                    "[PodcastSync] Could not discover RSS feed for '%s' — skipping",
                    podcast.name,
                )
                return {"slug": slug, "new_episodes": 0, "skipped": 0, "failed": 0}
            feed_url = discovered
            # Write the discovered URL back to Notion so future runs skip search
            if not dry_run and provider and hasattr(provider, "update_url") and podcast.url != feed_url:
                provider.update_url(podcast, feed_url)
                podcast.url = feed_url
            logger.info("[PodcastSync] Discovered feed URL: %s", feed_url)

        elif is_apple_podcasts_url(feed_url):
            resolved = resolve_feed_url(feed_url)
            if resolved != feed_url:
                logger.info("[PodcastSync] Resolved Apple Podcasts URL → %s", resolved)
                if not dry_run and provider and hasattr(provider, "update_url"):
                    provider.update_url(podcast, resolved)
                    podcast.url = resolved  # update in-memory for this run
                feed_url = resolved

        # ------------------------------------------------------------------
        # Step 2: Fetch and parse RSS feed (parse once, filter in Python)
        # ------------------------------------------------------------------
        logger.info("[PodcastSync] Fetching RSS feed: %s", feed_url)
        feed_xml = fetch_feed_xml(feed_url)
        all_feed_episodes = parse_episodes(feed_xml, max_age_days=None)
        if max_age_days is not None:
            from datetime import timedelta

            cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
            episodes = [ep for ep in all_feed_episodes if ep.pub_date >= cutoff]
        else:
            episodes = all_feed_episodes
        logger.info(
            "[PodcastSync] Feed has %d episodes (after age filter, %d total)", len(episodes), len(all_feed_episodes)
        )

        if not episodes:
            logger.info("[PodcastSync] No episodes to process for '%s'", podcast.name)
            return {"slug": slug, "new_episodes": 0, "skipped": 0, "failed": 0}

        # ------------------------------------------------------------------
        # Step 3: Diff against S3
        # ------------------------------------------------------------------
        s3 = S3Manager(bucket=bucket, playlist_id=slug)
        existing_ids = s3.list_existing_episodes()
        logger.info("[PodcastSync] S3 has %d existing episodes", len(existing_ids))

        # Load manifest to check for splice failures that need reprocessing.
        # Retries are capped at MAX_SPLICE_RETRIES (default 3) to avoid downloading
        # and transcribing a persistently-broken episode on every run indefinitely.
        manifest = s3.load_manifest()
        _max_splice_retries = settings.get("MAX_SPLICE_RETRIES")
        splice_retry_ids = {
            k
            for k, v in manifest.items()
            if isinstance(v, dict) and v.get("splice_failed") and v.get("splice_failed_count", 0) < _max_splice_retries
        }
        _splice_exhausted = {
            k
            for k, v in manifest.items()
            if isinstance(v, dict) and v.get("splice_failed") and v.get("splice_failed_count", 0) >= _max_splice_retries
        }
        if splice_retry_ids:
            logger.info(
                "[PodcastSync] %d episode(s) marked for splice retry: %s",
                len(splice_retry_ids),
                splice_retry_ids,
            )
        if _splice_exhausted:
            logger.warning(
                "[PodcastSync] %d episode(s) have exhausted splice retries "
                "(MAX_SPLICE_RETRIES=%d) and will not be retried: %s",
                len(_splice_exhausted),
                _max_splice_retries,
                _splice_exhausted,
            )

        # Build (episode, episode_id) pairs for candidates
        candidates: list[tuple[EpisodeMeta, str]] = []
        skipped = 0
        for ep in episodes:
            ep_id = episode_id_from_guid(ep.guid)
            # Permanently exhausted — never uploaded, no infinite retry loop
            if ep_id in _splice_exhausted:
                logger.warning(
                    "[PodcastSync] Skipping %s permanently — splice failed %d time(s), "                     "reached MAX_SPLICE_RETRIES=%d; manual intervention required",
                    ep_id,
                    manifest.get(ep_id, {}).get("splice_failed_count", 0),
                    _max_splice_retries,
                )
                skipped += 1
                continue
            # Successfully published in a prior run — skip unless eligible for splice retry.
            # The splice_retry_ids bypass handles both new behaviour (episode never
            # uploaded) and the migration case where the old code uploaded the original
            # file with ads and we still need to re-process it.
            if ep_id in existing_ids and ep_id not in splice_retry_ids:
                skipped += 1
                logger.debug("[PodcastSync] Already in S3, skipping: %s", ep_id)
                continue
            # New episode, or previously splice-failed (under or over retries threshold)
            if ep_id in splice_retry_ids:
                logger.info(
                    "[PodcastSync] Re-queuing %s for splice retry (run attempt %d/%d)",
                    ep_id,
                    manifest.get(ep_id, {}).get("splice_failed_count", 0) + 1,
                    _max_splice_retries,
                )
            candidates.append((ep, ep_id))
            if len(candidates) >= max_episodes:
                break

        logger.info(
            "[PodcastSync] %d new candidates (skipped %d already in S3)",
            len(candidates),
            skipped,
        )

        if dry_run:
            for ep, ep_id in candidates:
                logger.info("[DRY-RUN] Would download + upload: %s (%s)", ep.title, ep_id)
            return {
                "slug": slug,
                "new_episodes": len(candidates),
                "skipped": skipped,
                "failed": 0,
            }

        # ------------------------------------------------------------------
        # Step 4: Download → remove ads → upload
        # Episodes are processed in a thread pool (PODCAST_EPISODE_WORKERS).
        # Workers are IO-bound (download, Transcribe, Bedrock) so even workers=3
        # yields significant throughput gains with minimal resource cost.
        # Manifest updates are serialised with a lock.
        # ------------------------------------------------------------------
        manifest_lock = threading.Lock()

        new_count = 0
        failed_count = 0
        uploaded_pairs: list[tuple[EpisodeMeta, str]] = []

        age_days = max_age_days if max_age_days else 30

        def _process_episode(ep: EpisodeMeta, ep_id: str) -> dict:
            """Download, clean, and upload one episode.  Returns a result dict."""
            thread_s3 = S3Manager(bucket=bucket, playlist_id=slug)
            # Each attempt re-downloads from the CDN so a transiently corrupt
            # SSAI-stitched file (different bytes per request) doesn't block
            # the episode permanently.  Both the transcript AND ad-segment
            # caches (keyed by ep_id, independent of file bytes) are left
            # intact across attempts -- a retry only re-pays download +
            # ffprobe-validate + splice cost.  Transcribe + Bedrock detection
            # run at most once per episode, even across separate cron runs
            # (remove_ads() itself checks the ad-segment cache and skips
            # straight to splicing when present) -- this is what decouples
            # splice reliability from the expensive detection pipeline.
            cdn = detect_cdn(ep.url)
            _splice_max_attempts = _splice_attempts_for_cdn(cdn)

            original_path: str | None = None
            cleaned_path: str | None = None
            ad_segments: list = []
            summary = ""
            splice_failed = False
            fail_reason = ""

            try:
                for attempt in range(1, _splice_max_attempts + 1):
                    if attempt > 1:
                        logger.info(
                            "[PodcastSync] Retrying with fresh download for %s (attempt %d/%d, cdn=%s)",
                            ep_id, attempt, _splice_max_attempts, cdn,
                        )
                        if original_path and os.path.exists(original_path):
                            with contextlib.suppress(OSError):
                                os.remove(original_path)

                    original_path = download_episode(ep.url, ep_id, tmp_dir)

                    # ffprobe pre-flight: catch a corrupt/truncated SSAI-stitched
                    # fetch before paying for Transcribe + Bedrock.
                    is_valid, invalid_reason = validate_audio_file(original_path)
                    if not is_valid:
                        splice_failed = True
                        fail_reason = f"download validation failed: {invalid_reason}"
                        logger.warning(
                            "[PodcastSync] Downloaded file failed ffprobe validation for %s "                             "on attempt %d/%d (cdn=%s): %s%s",
                            ep_id,
                            attempt,
                            _splice_max_attempts,
                            cdn,
                            invalid_reason,
                            " — will retry with fresh download" if attempt < _splice_max_attempts else " — all attempts exhausted",
                        )
                        continue  # skip Transcribe/Bedrock entirely — retry download

                    logger.info(
                        "[PodcastSync] Running ad removal for %s (attempt %d/%d, cdn=%s)",
                        ep_id, attempt, _splice_max_attempts, cdn,
                    )
                    cleaned_path, ad_segments, summary = remove_ads(
                        original_path,
                        ep_id,
                        tmp_dir,
                        ad_hints=podcast.ad_hints,
                        trim_music_intro=podcast.trim_music_intro,
                        trim_music_outro=podcast.trim_music_outro,
                        min_music_intro_secs=podcast.min_music_intro_secs,
                        min_music_outro_secs=podcast.min_music_outro_secs,
                        episode_title=ep.title,
                        duration_secs=ep.duration,
                        cache_namespace=slug,
                    )

                    splice_failed = bool(ad_segments) and cleaned_path == original_path
                    if not splice_failed:
                        break  # success — exit retry loop

                    fail_reason = f"splice crashed ({len(ad_segments)} ads detected but original returned)"
                    logger.warning(
                        "[PodcastSync] Splice failed for %s on attempt %d/%d (cdn=%s) "                         "(%d ads detected but original returned)%s",
                        ep_id,
                        attempt,
                        _splice_max_attempts,
                        cdn,
                        len(ad_segments),
                        " — will retry with fresh download" if attempt < _splice_max_attempts else " — all attempts exhausted",
                    )

                if splice_failed:
                    # All attempts exhausted — do NOT upload the original with ads.
                    # The episode is absent from S3, so the next scheduled run will
                    # naturally re-queue it (up to the cross-run MAX_SPLICE_RETRIES cap).
                    logger.error(
                        "[PodcastSync] Splice permanently failed for %s after %d attempt(s) (cdn=%s, reason=%s) "                         "— episode will NOT be published until splice succeeds",
                        ep_id, _splice_max_attempts, cdn, fail_reason,
                    )
                    # cleaned_path == original_path here (that's the splice-failed
                    # detection criterion), so only the original download needs removing.
                    if original_path and os.path.exists(original_path):
                        with contextlib.suppress(OSError):
                            os.remove(original_path)
                    return {
                        "ok": True,
                        "ep": ep,
                        "ep_id": ep_id,
                        "uploaded": False,
                        "splice_failed": True,
                        "ads_detected": len(ad_segments),
                        "cdn": cdn,
                        "fail_reason": fail_reason,
                    }

                # Evaluate ad removal quality on the cleaned file (opt-in via env var)
                if cleaned_path != original_path:
                    try:
                        from ad_evaluator import evaluate_ad_removal

                        evaluate_ad_removal(
                            cleaned_mp3=cleaned_path,
                            episode_id=ep_id,
                            slug=slug,
                            original_ad_segments=ad_segments,
                        )
                    except Exception as eval_exc:
                        logger.warning("[PodcastSync] Ad evaluation failed for %s: %s", ep_id, eval_exc)

                # Clean up the original if ad removal produced a separate file
                if cleaned_path != original_path and original_path and os.path.exists(original_path):
                    os.remove(original_path)

                logger.info("[PodcastSync] Uploading %s to S3", ep_id)
                thread_s3.upload_episode(cleaned_path, ep_id, age_days)

                try:
                    file_size = os.path.getsize(cleaned_path)
                except OSError:
                    file_size = 0

                if cleaned_path and os.path.exists(cleaned_path):
                    os.remove(cleaned_path)

                logger.info("[PodcastSync] Done: %s", ep_id)
                ads_removed = bool(ad_segments) and cleaned_path != original_path
                return {
                    "ok": True,
                    "ep": ep,
                    "ep_id": ep_id,
                    "uploaded": True,
                    "file_size": file_size,
                    "summary": summary,
                    "ads_removed": ads_removed,
                    "splice_failed": False,
                    "cdn": cdn,
                }

            except Exception as exc:
                logger.error("[PodcastSync] Failed %s: %s", ep_id, exc)
                for p in (original_path, cleaned_path):
                    if p and os.path.exists(p):
                        with contextlib.suppress(OSError):
                            os.remove(p)
                return {"ok": False, "ep_id": ep_id, "error": exc}

        workers = settings.get("PODCAST_EPISODE_WORKERS")
        workers = max(1, min(workers, len(candidates)))  # clamp to [1, n_candidates]
        logger.info("[PodcastSync] Processing %d candidate(s) with %d worker(s)", len(candidates), workers)

        splice_failed_this_run = 0

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_process_episode, ep, ep_id): (ep, ep_id) for ep, ep_id in candidates}
            for future in as_completed(futures):
                result = future.result()
                if result["ok"] and result.get("uploaded", True):
                    # Episode successfully cleaned and uploaded
                    with manifest_lock:
                        ep, ep_id = result["ep"], result["ep_id"]
                        _prev_fail_count = manifest.get(ep_id, {}).get("splice_failed_count", 0)
                        manifest[ep_id] = {
                            "size": result["file_size"],
                            "title": ep.title,
                            "guid": ep.guid,
                            "pub_date": ep.pub_date.isoformat(),
                            "duration": ep.duration,
                            "ads_removed": result.get("ads_removed", False),
                            "splice_failed": False,
                            # Preserve historical count so the exhaustion cap still works
                            "splice_failed_count": _prev_fail_count,
                            "cdn": result.get("cdn", "unknown"),
                        }
                        if result.get("summary") and result["summary"] not in REMOVE_ADS_ERROR_CODES:
                            manifest[ep_id]["summary"] = result["summary"]
                        new_count += 1
                        uploaded_pairs.append((ep, ep_id))
                elif result["ok"] and result.get("splice_failed"):
                    # Splice failed after all per-run attempts — episode NOT uploaded.
                    # Write attempt count to manifest so the cross-run cap is enforced.
                    # Episode is absent from S3, so the next run naturally re-queues it.
                    with manifest_lock:
                        ep, ep_id = result["ep"], result["ep_id"]
                        _prev_fail_count = manifest.get(ep_id, {}).get("splice_failed_count", 0)
                        manifest[ep_id] = {
                            **{k: v for k, v in manifest.get(ep_id, {}).items()
                               if k not in ("size", "ads_removed", "splice_failed", "splice_failed_count")},
                            "title": ep.title,
                            "guid": ep.guid,
                            "pub_date": ep.pub_date.isoformat(),
                            "duration": ep.duration,
                            "splice_failed": True,
                            "splice_failed_count": _prev_fail_count + 1,
                            "cdn": result.get("cdn", "unknown"),
                            "fail_reason": result.get("fail_reason", ""),
                        }
                        splice_failed_this_run += 1
                else:
                    with manifest_lock:
                        failed_count += 1

        # Persist manifest whenever something changed — uploads or splice-fail count updates
        if new_count > 0 or splice_failed_this_run > 0:
            s3.save_manifest(manifest)

        # ------------------------------------------------------------------
        # Step 5: Rebuild feed.xml
        # ------------------------------------------------------------------
        if new_count > 0 or skipped > 0:
            # Collect all episodes currently in S3 for the feed
            all_existing_ids = s3.list_existing_episodes()

            # Build id→EpisodeMeta from the FULL (unfiltered) feed (already parsed above)
            id_to_ep: dict[str, EpisodeMeta] = {}
            for ep in all_feed_episodes:
                eid = episode_id_from_guid(ep.guid)
                id_to_ep[eid] = ep

            feed_episodes: list[EpisodeMeta] = []
            feed_ep_ids: list[str] = []
            for eid in sorted(all_existing_ids):
                if eid in id_to_ep:
                    feed_episodes.append(id_to_ep[eid])
                    feed_ep_ids.append(eid)

            # Sort newest-first by pub_date
            pairs = sorted(
                zip(feed_episodes, feed_ep_ids, strict=True),
                key=lambda x: x[0].pub_date,
                reverse=True,
            )
            if pairs:
                feed_episodes, feed_ep_ids = zip(*pairs, strict=True)
                feed_episodes = list(feed_episodes)
                feed_ep_ids = list(feed_ep_ids)
            else:
                feed_episodes, feed_ep_ids = [], []

            # Use manifest for file sizes (avoids N head_object calls).
            # Fall back to head_object only for episodes missing from manifest.
            ep_sizes: dict[str, int] = {}
            missing_from_manifest: list[str] = []
            for eid in feed_ep_ids:
                if eid in manifest and "size" in manifest[eid]:
                    ep_sizes[eid] = manifest[eid]["size"]
                else:
                    missing_from_manifest.append(eid)

            if missing_from_manifest:
                logger.info(
                    "[PodcastSync] metadata backfill for %d episodes not in manifest",
                    len(missing_from_manifest),
                )
                for eid in missing_from_manifest:
                    s3_key = f"{slug}/episodes/{eid}.mp3"
                    entry: dict = manifest.setdefault(eid, {})

                    # Backfill size via head_object
                    try:
                        size = s3.get_object_size(s3_key)
                        ep_sizes[eid] = size
                        entry["size"] = size
                    except Exception:
                        ep_sizes[eid] = 0

                    # Backfill episode metadata from the RSS feed (if available)
                    if eid in id_to_ep and not entry.get("title"):
                        ep_meta = id_to_ep[eid]
                        entry.update(
                            {
                                "title": ep_meta.title,
                                "guid": ep_meta.guid,
                                "pub_date": ep_meta.pub_date.isoformat(),
                                "duration": ep_meta.duration,
                            }
                        )
                        logger.debug(
                            "[PodcastSync] Backfilled metadata for %s: %s",
                            eid,
                            ep_meta.title,
                        )

                # Persist the backfilled manifest entries
                s3.save_manifest(manifest)

            logger.info("[PodcastSync] Generating feed.xml with %d episodes", len(feed_episodes))
            # The episodes and manifest are already uploaded at this point, so a
            # feed-build fault must not propagate: it would abort the run, skip
            # the Notion write-back, and discard the record of everything this
            # run paid Transcribe and Bedrock for.  The next run rebuilds the
            # feed from S3, so degrading to a warning is recoverable.
            try:
                channel_thumbnail = parse_channel_thumbnail(feed_xml)
                xml_content = _build_podcast_feed_xml(
                    podcast,
                    feed_episodes,
                    feed_ep_ids,
                    cloudfront_base,
                    slug,
                    ep_sizes,
                    channel_thumbnail=channel_thumbnail,
                    language=podcast.language,
                    manifest=manifest,
                )
                s3.upload_feed(xml_content)
                logger.info("[PodcastSync] feed.xml uploaded")
            except Exception as exc:
                logger.error(
                    "[PodcastSync] feed.xml generation failed for '%s': %s — "
                    "%d episode(s) are uploaded and will appear once the feed rebuilds next run",
                    podcast.name,
                    exc,
                    new_count,
                    exc_info=True,
                )

        # Update Notion status based on this run's outcomes.
        # splice_failed_this_run is used (not the historical manifest total) so
        # a podcast that previously had a splice failure but succeeded today
        # correctly shows 'Done' rather than a stale 'Splice Failed'.
        if splice_failed_this_run > 0 and provider:
            try:
                provider.update_status(podcast, "Splice Failed")
            except Exception as exc:
                logger.warning("[PodcastSync] Failed to update Notion status: %s", exc)
        elif new_count > 0 and provider:
            try:
                provider.update_status(podcast, "Done")
            except Exception as exc:
                logger.warning("[PodcastSync] Failed to update Notion status: %s", exc)

        elapsed = time.monotonic() - _run_start
        logger.info(
            "=== PODCAST SUMMARY === slug=%s new=%d skipped=%d failed=%d splice_failed=%d elapsed=%.1fs",
            slug,
            new_count,
            skipped,
            failed_count,
            splice_failed_this_run,
            elapsed,
        )
        return {
            "slug": slug,
            "new_episodes": new_count,
            "skipped": skipped,
            "failed": failed_count,
            "splice_failed": splice_failed_this_run,
            "elapsed_seconds": round(elapsed, 1),
        }

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.debug("[PodcastSync] Cleaned up tmp dir %s", tmp_dir)
