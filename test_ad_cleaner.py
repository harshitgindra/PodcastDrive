#!/usr/bin/env python3
"""Ad-cleaner test harness — uses the same remove_ads() as production.

Downloads one episode, removes ads via the canonical remove_ads() pipeline
(transcription, detection, silence snapping, splicing, optional summary),
and saves a cleaned MP3 locally for manual listening verification.

All behaviour is controlled by env vars in config.env — identical to production:
  - REMOVE_ADS / REMOVE_ADS_DRY_RUN
  - AD_SNAP_TO_SILENCE / AD_VERIFY_THRESHOLD_SECS
  - BEDROCK_MODEL_ID / BEDROCK_DETECT_MODEL_ID
  - TRANSCRIBE_CACHE_ENABLED / TRANSCRIBE_CACHE_PREFIX
  - GENERATE_SUMMARIES
  - MAX_AD_SEGMENT_SECS
  - SPLICE_LOUDNORM
  - AD_TRANSCRIBE_WINDOWS

Usage examples:
    # YouTube video (direct)
    python3 test_ad_cleaner.py "https://www.youtube.com/watch?v=LLjpnubsOWc"

    # YouTube channel handle — fetches the latest upload
    python3 test_ad_cleaner.py "@aliabdaal"

    # YouTube playlist ID — fetches the latest item
    python3 test_ad_cleaner.py "PLEVkQGIATCXI1F2qs0slVE2MScaj1cSM0"

    # RSS feed URL
    python3 test_ad_cleaner.py "https://feeds.megaphone.fm/WWO4510910710"

    # Podcast name — searches iTunes for the feed, then fetches the latest episode
    python3 test_ad_cleaner.py "The Tim Ferriss Show"

Options:
    --out-dir DIR   Where to save output files (default: ./test_output)
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import ssl
import tempfile
import urllib.request
import certifi
from pathlib import Path

# ── path setup ────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT / "src"))

import yt_dlp
from downloader import download_and_convert
from podcast_downloader import (
    episode_id_from_guid,
    fetch_feed_xml,
    parse_episodes,
    resolve_feed_url,
    search_feed_url_by_name,
)
from ad_remover import remove_ads

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# suppress noisy library loggers
for _noisy in ("ad_remover", "ad_evaluator", "downloader", "podcast_downloader",
               "botocore", "boto3", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


# ═════════════════════════════════════════════════════════════════════════════
# Source resolution
# ═════════════════════════════════════════════════════════════════════════════

def _is_youtube(url: str) -> bool:
    return bool(re.search(r"youtube\.com|youtu\.be", url))


def _is_playlist_or_channel(source: str) -> bool:
    """True for @handle, PLxxxxxx, or youtube.com/playlist / @channel URLs."""
    if source.startswith("@"):
        return True
    if re.match(r"^PL[A-Za-z0-9_-]{10,}", source):
        return True
    if "youtube.com/playlist" in source or "youtube.com/@" in source:
        return True
    return False


def resolve_youtube_latest(source: str) -> tuple[str, str, str]:
    """Return (video_url, video_id, title) for the latest video in a channel/playlist,
    or for a direct video URL.
    """
    if source.startswith("@"):
        ydl_url = f"https://www.youtube.com/{source}/videos"
    elif re.match(r"^PL[A-Za-z0-9_-]{10,}", source):
        ydl_url = f"https://www.youtube.com/playlist?list={source}"
    else:
        ydl_url = source

    is_channel_or_playlist = _is_playlist_or_channel(source)

    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    if is_channel_or_playlist:
        ydl_opts["playlistend"] = 1
        ydl_opts["extract_flat"] = "in_playlist"

    print(f"  Resolving YouTube source: {source}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(ydl_url, download=False)

    if not info:
        raise RuntimeError(f"yt-dlp could not resolve: {source}")

    if info.get("_type") in ("playlist", "channel"):
        entries = info.get("entries") or []
        if not entries:
            raise RuntimeError(f"No videos found for: {source}")
        entry = entries[0]
        video_id = entry.get("id") or entry.get("url", "").split("v=")[-1]
        title = entry.get("title", video_id)
        video_url = f"https://www.youtube.com/watch?v={video_id}"
    else:
        video_id = info.get("id", "unknown")
        title = info.get("title", video_id)
        video_url = f"https://www.youtube.com/watch?v={video_id}"

    return video_url, video_id, title


def resolve_rss_latest(feed_url: str) -> tuple[str, str, str]:
    """Return (episode_mp3_url, episode_id, title) for the latest RSS episode."""
    feed_url = resolve_feed_url(feed_url)
    xml_bytes = fetch_feed_xml(feed_url)
    episodes = parse_episodes(xml_bytes)
    if not episodes:
        raise RuntimeError(f"No episodes found in feed: {feed_url}")
    latest = episodes[0]
    ep_id = episode_id_from_guid(latest.guid, "")
    return latest.url, ep_id, latest.title


def resolve_source(source: str) -> tuple[str, str, str, str]:
    """Determine source type and return (source_type, download_url, episode_id, title)."""
    # Direct YouTube video
    if _is_youtube(source) and not _is_playlist_or_channel(source):
        video_id = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", source)
        if video_id:
            vid = video_id.group(1)
            return "youtube", source, vid, vid

    # YouTube channel / playlist
    if _is_youtube(source) or _is_playlist_or_channel(source):
        url, vid, title = resolve_youtube_latest(source)
        return "youtube", url, vid, title

    # Direct RSS/HTTP URL
    if source.startswith("http"):
        url, ep_id, title = resolve_rss_latest(source)
        return "rss", url, ep_id, title

    # Podcast name → iTunes search → RSS
    print(f"  Searching iTunes for podcast: {source!r}")
    feed_url = search_feed_url_by_name(source)
    if feed_url:
        url, ep_id, title = resolve_rss_latest(feed_url)
        return "rss", url, ep_id, title

    raise RuntimeError(
        f"Could not resolve source: {source!r}\n"
        "Try passing a direct YouTube URL, @channelHandle, podcast name, or RSS URL."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Download
# ═════════════════════════════════════════════════════════════════════════════

def download_episode(
    source_type: str,
    download_url: str,
    episode_id: str,
    out_dir: str,
) -> str:
    """Download the episode and return the local MP3 path."""
    mp3_path = os.path.join(out_dir, f"{episode_id}.mp3")
    if os.path.exists(mp3_path):
        print(f"  Using cached download: {mp3_path}")
        return mp3_path

    if source_type == "youtube":
        return download_and_convert(download_url, episode_id, out_dir)

    # RSS: direct HTTP download
    print(f"  Downloading RSS episode: {download_url[:80]}...")
    req = urllib.request.Request(
        download_url,
        headers={"User-Agent": "PodcastDrive/1.0"},
    )
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp, \
            open(mp3_path, "wb") as f:
        while chunk := resp.read(65536):
            f.write(chunk)
    return mp3_path


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _fmt(secs: float) -> str:
    """Format seconds as MM:SS."""
    m, s = divmod(int(secs), 60)
    return f"{m:02d}:{s:02d}"


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the ad cleaner on one episode end-to-end (uses remove_ads())",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("source",
        help="YouTube URL / @handle / playlist ID / RSS URL / podcast name")
    parser.add_argument("--out-dir", default="test_output",
        help="Output directory (default: ./test_output)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    W = 70
    print()
    print("═" * W)
    print("  AD CLEANER TEST HARNESS")
    print("  (uses remove_ads() — identical to production pipeline)")
    print("═" * W)
    print()

    # ── Step 1: Resolve source ────────────────────────────────────────────────
    print("[1/3] Resolving source...")
    source_type, download_url, episode_id, title = resolve_source(args.source)
    print(f"  Episode : {title}")
    print(f"  ID      : {episode_id}")
    print(f"  Type    : {source_type}")

    # ── Step 2: Download ──────────────────────────────────────────────────────
    print()
    print("[2/3] Downloading episode...")
    original_mp3 = download_episode(source_type, download_url, episode_id, str(out_dir))
    size_mb = os.path.getsize(original_mp3) / 1_048_576
    print(f"  Saved → {original_mp3}  ({size_mb:.1f} MB)")

    # ── Step 3: Remove ads (uses the same remove_ads() as production) ─────────
    print()
    print("[3/3] Removing ads...")
    print("  (transcription, detection, and splicing via remove_ads() — same as production)")

    tmp_dir = tempfile.mkdtemp(prefix=f"test-ad-{episode_id}-")
    try:
        cleaned_mp3, ad_segs, summary = remove_ads(
            original_mp3,
            episode_id,
            tmp_dir,
            ad_hints="",
        )
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"\n  ERROR: remove_ads() raised an exception: {exc}")
        sys.exit(1)

    if cleaned_mp3 == original_mp3:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print()
        print("  NO ADS REMOVED — remove_ads() returned the original file.")
        print("  This means either: no ads detected, REMOVE_ADS=false, or an error occurred.")
        print("  Check the log output above for details.")
        print(f"  Original file: {original_mp3}")
        # Copy original to out_dir for consistency
        final_output = str(out_dir / f"{episode_id}_original.mp3")
        shutil.copy2(original_mp3, final_output)
        print(f"  Copied to: {final_output}")
        sys.exit(0)

    # Move cleaned file to out_dir
    final_output = str(out_dir / f"{episode_id}_clean.mp3")
    shutil.move(cleaned_mp3, final_output)
    cleaned_mp3 = final_output
    shutil.rmtree(tmp_dir, ignore_errors=True)

    total_removed = sum(s["end"] - s["start"] for s in ad_segs)
    print(f"  Detected and removed {len(ad_segs)} ad segment(s) — {total_removed:.0f}s total")
    for i, s in enumerate(ad_segs, 1):
        mins_s = int(s['start']) // 60
        secs_s = int(s['start']) % 60
        mins_e = int(s['end']) // 60
        secs_e = int(s['end']) % 60
        print(f"    #{i}: {mins_s:02d}:{secs_s:02d} → {mins_e:02d}:{secs_e:02d}  ({s['end']-s['start']:.0f}s)")

    if summary:
        print(f"\n  Summary: {summary}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("═" * W)
    print(f"  DONE")
    print(f"  Episode  : {title}")
    print(f"  Removed  : {len(ad_segs)} segment(s), {total_removed:.0f}s total")
    print(f"  Output   : {cleaned_mp3}")
    print("═" * W)


if __name__ == "__main__":
    main()
