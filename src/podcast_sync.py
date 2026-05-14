"""RSS podcast feed sync pipeline.

Orchestrates the full pipeline for ``Source=Podcast`` Notion entries:
  1. Resolve Apple Podcasts / iTunes URLs → real RSS feed URL (write-back).
  2. Fetch and parse the RSS feed.
  3. Diff episodes against existing S3 objects.
  4. Download new episodes, remove ads, upload to S3.
  5. Generate and upload ``feed.xml``.
  6. Update Notion status / last-run timestamp.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString

from ad_remover import remove_ads
from config_provider import PodcastConfig
from podcast_downloader import (
    EpisodeMeta,
    download_episode,
    episode_id_from_guid,
    fetch_feed_xml,
    is_apple_podcasts_url,
    parse_episodes,
    resolve_feed_url,
)
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


def _build_podcast_feed_xml(
    podcast: PodcastConfig,
    episodes: list[EpisodeMeta],
    episode_ids: list[str],
    cloudfront_base: str,
    slug: str,
) -> str:
    """Generate a minimal RSS 2.0 feed for *podcast* from cleaned episode list.

    Args:
        podcast:         Podcast configuration (name, url).
        episodes:        EpisodeMeta objects for episodes in the feed.
        episode_ids:     Parallel list of stable S3 episode IDs.
        cloudfront_base: CloudFront distribution base URL (no trailing slash).
        slug:            S3 folder slug for this podcast.

    Returns:
        Pretty-printed RSS XML string.
    """
    import xml.etree.ElementTree as ET

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = podcast.name
    ET.SubElement(channel, "link").text = podcast.url
    ET.SubElement(channel, "description").text = podcast.name
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "generator").text = "PodcastDrive"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )
    ET.SubElement(channel, f"{{{_ITUNES_NS}}}author").text = podcast.name
    ET.SubElement(channel, f"{{{_ITUNES_NS}}}explicit").text = "no"

    for ep, ep_id in zip(episodes, episode_ids):
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = ep.title

        guid_el = ET.SubElement(item, "guid")
        guid_el.set("isPermaLink", "false")
        guid_el.text = ep.guid

        cf_url = f"{cloudfront_base}/{slug}/episodes/{ep_id}.mp3"
        enc = ET.SubElement(item, "enclosure")
        enc.set("url", cf_url)
        enc.set("length", "0")  # size unknown until after upload; Overcast ignores
        enc.set("type", "audio/mpeg")

        ET.SubElement(item, "pubDate").text = format_datetime(ep.pub_date)
        ET.SubElement(item, f"{{{_ITUNES_NS}}}duration").text = _format_duration(
            ep.duration
        )
        ET.SubElement(item, f"{{{_ITUNES_NS}}}explicit").text = "no"

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
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        raise ValueError("S3_BUCKET environment variable must be set")
    cloudfront_base = os.environ.get("CLOUDFRONT_BASE", "")
    if not cloudfront_base:
        raise ValueError("CLOUDFRONT_BASE environment variable must be set")

    max_age_days = podcast.max_age_days
    if max_age_days is None:
        env_val = os.environ.get("MAX_AGE_DAYS")
        max_age_days = int(env_val) if env_val else None

    max_episodes = podcast.max_downloads
    if max_episodes is None:
        env_val = os.environ.get("PODCAST_MAX_EPISODES")
        max_episodes = int(env_val) if env_val else 5  # conservative default

    slug = _podcast_slug(podcast.name)
    tmp_dir = f"/tmp/podcast-{slug}"

    logger.info(
        "[PodcastSync] Starting '%s' (slug=%s, max_episodes=%s, max_age_days=%s, dry_run=%s)",
        podcast.name, slug, max_episodes, max_age_days, dry_run,
    )

    try:
        os.makedirs(tmp_dir, exist_ok=True)

        # ------------------------------------------------------------------
        # Step 1: Resolve Apple Podcasts URL → real RSS feed URL
        # ------------------------------------------------------------------
        feed_url = podcast.url
        if is_apple_podcasts_url(feed_url):
            resolved = resolve_feed_url(feed_url)
            if resolved != feed_url:
                logger.info(
                    "[PodcastSync] Resolved Apple Podcasts URL → %s", resolved
                )
                if not dry_run and provider and hasattr(provider, "update_url"):
                    provider.update_url(podcast, resolved)
                    podcast.url = resolved  # update in-memory for this run
                feed_url = resolved

        # ------------------------------------------------------------------
        # Step 2: Fetch and parse RSS feed
        # ------------------------------------------------------------------
        logger.info("[PodcastSync] Fetching RSS feed: %s", feed_url)
        feed_xml = fetch_feed_xml(feed_url)
        episodes = parse_episodes(feed_xml, max_age_days=max_age_days)
        logger.info("[PodcastSync] Feed has %d episodes (after age filter)", len(episodes))

        if not episodes:
            logger.info("[PodcastSync] No episodes to process for '%s'", podcast.name)
            return {"slug": slug, "new_episodes": 0, "skipped": 0, "failed": 0}

        # ------------------------------------------------------------------
        # Step 3: Diff against S3
        # ------------------------------------------------------------------
        s3 = S3Manager(bucket=bucket, playlist_id=slug)
        existing_ids = s3.list_existing_episodes()
        logger.info("[PodcastSync] S3 has %d existing episodes", len(existing_ids))

        # Build (episode, episode_id) pairs for candidates
        candidates: list[tuple[EpisodeMeta, str]] = []
        skipped = 0
        for ep in episodes:
            ep_id = episode_id_from_guid(ep.guid, slug)
            if ep_id in existing_ids:
                skipped += 1
                logger.debug("[PodcastSync] Already in S3, skipping: %s", ep_id)
                continue
            candidates.append((ep, ep_id))
            if len(candidates) >= max_episodes:
                break

        logger.info(
            "[PodcastSync] %d new candidates (skipped %d already in S3)",
            len(candidates), skipped,
        )

        if dry_run:
            for ep, ep_id in candidates:
                logger.info(
                    "[DRY-RUN] Would download + upload: %s (%s)", ep.title, ep_id
                )
            return {
                "slug": slug,
                "new_episodes": len(candidates),
                "skipped": skipped,
                "failed": 0,
            }

        # ------------------------------------------------------------------
        # Step 4: Download → remove ads → upload
        # ------------------------------------------------------------------
        new_count = 0
        failed_count = 0
        uploaded_pairs: list[tuple[EpisodeMeta, str]] = []

        for ep, ep_id in candidates:
            logger.info("[PodcastSync] Processing episode: %s (%s)", ep.title, ep_id)
            try:
                mp3_path = download_episode(ep.url, ep_id, tmp_dir)

                logger.info("[PodcastSync] Running ad removal for %s", ep_id)
                mp3_path = remove_ads(mp3_path, ep_id, tmp_dir)

                logger.info("[PodcastSync] Uploading %s to S3", ep_id)
                age_days = max_age_days if max_age_days else 30
                s3.upload_episode(mp3_path, ep_id, age_days)
                os.remove(mp3_path)

                new_count += 1
                uploaded_pairs.append((ep, ep_id))
                logger.info("[PodcastSync] Done: %s", ep_id)

            except Exception as exc:
                failed_count += 1
                logger.error("[PodcastSync] Failed %s: %s", ep_id, exc)

        # ------------------------------------------------------------------
        # Step 5: Rebuild feed.xml
        # ------------------------------------------------------------------
        if new_count > 0 or skipped > 0:
            # Collect all episodes currently in S3 for the feed
            all_existing_ids = s3.list_existing_episodes()

            # Build a map of ep_id → EpisodeMeta from what we know
            id_to_ep: dict[str, EpisodeMeta] = {}
            for ep in episodes:
                eid = episode_id_from_guid(ep.guid, slug)
                id_to_ep[eid] = ep

            feed_episodes: list[EpisodeMeta] = []
            feed_ep_ids: list[str] = []
            for eid in sorted(all_existing_ids):
                if eid in id_to_ep:
                    feed_episodes.append(id_to_ep[eid])
                    feed_ep_ids.append(eid)

            # Sort newest-first by pub_date
            pairs = sorted(
                zip(feed_episodes, feed_ep_ids),
                key=lambda x: x[0].pub_date,
                reverse=True,
            )
            if pairs:
                feed_episodes, feed_ep_ids = zip(*pairs)
                feed_episodes = list(feed_episodes)
                feed_ep_ids = list(feed_ep_ids)
            else:
                feed_episodes, feed_ep_ids = [], []

            logger.info(
                "[PodcastSync] Generating feed.xml with %d episodes", len(feed_episodes)
            )
            xml_content = _build_podcast_feed_xml(
                podcast, feed_episodes, feed_ep_ids, cloudfront_base, slug
            )
            s3.upload_feed(xml_content)
            logger.info("[PodcastSync] feed.xml uploaded")

        logger.info(
            "[PodcastSync] Done '%s': %d new, %d skipped, %d failed",
            podcast.name, new_count, skipped, failed_count,
        )
        return {
            "slug": slug,
            "new_episodes": new_count,
            "skipped": skipped,
            "failed": failed_count,
        }

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.debug("[PodcastSync] Cleaned up tmp dir %s", tmp_dir)
