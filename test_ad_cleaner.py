#!/usr/bin/env python3
"""Manual ad-cleaner test harness.

Downloads one episode, removes ads, verifies, and saves a cleaned MP3 locally
so you can listen and confirm the cuts are good.

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

    # Reuse a cached transcript (skip Transcribe cost)
    python3 test_ad_cleaner.py "@aliabdaal" --skip-transcribe

Options:
    --max-iter N    Max retry iterations if residuals found (default: 2)
    --skip-transcribe  Reuse cached transcript from eval/transcripts/
    --no-snap          Disable silence-boundary snapping of cut points
    --out-dir DIR      Where to save output files (default: ./test_output)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import urllib.request
import ssl
import certifi
from pathlib import Path
from datetime import datetime, timezone

# ── path setup ────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT / "src"))

import yt_dlp
from downloader import download_and_convert
from podcast_downloader import (
    fetch_feed_xml,
    parse_episodes,
    resolve_feed_url,
    search_feed_url_by_name,
)
from ad_remover import (
    _merge_overlapping_ads,
    detect_ads,
    detect_silence,
    snap_ad_boundaries,
    splice_audio,
    transcribe_audio,
)
from ad_evaluator import _translate_cleaned_to_original

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

# ── directories ───────────────────────────────────────────────────────────────
_TRANSCRIPT_CACHE = _ROOT / "eval" / "transcripts"
_TRANSCRIPT_CACHE.mkdir(parents=True, exist_ok=True)


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
    # Construct the URL yt-dlp should query
    if source.startswith("@"):
        ydl_url = f"https://www.youtube.com/{source}/videos"
    elif re.match(r"^PL[A-Za-z0-9_-]{10,}", source):
        ydl_url = f"https://www.youtube.com/playlist?list={source}"
    else:
        ydl_url = source  # direct video or playlist URL as-is

    is_channel_or_playlist = _is_playlist_or_channel(source)

    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    if is_channel_or_playlist:
        # Only extract the first (most recent) entry
        ydl_opts["playlistend"] = 1
        ydl_opts["extract_flat"] = "in_playlist"

    print(f"  Resolving YouTube source: {source}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(ydl_url, download=False)

    if not info:
        raise RuntimeError(f"yt-dlp could not resolve: {source}")

    # Unwrap playlist/channel to first entry
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
    feed_url = resolve_feed_url(feed_url)   # resolve Apple Podcasts links
    xml_bytes = fetch_feed_xml(feed_url)
    episodes = parse_episodes(xml_bytes)
    if not episodes:
        raise RuntimeError(f"No episodes found in feed: {feed_url}")
    latest = episodes[0]
    ep_id = re.sub(r"[^A-Za-z0-9_-]", "-", latest.guid)[:64].strip("-")
    return latest.url, ep_id, latest.title


def resolve_source(source: str) -> tuple[str, str, str, str]:
    """Determine source type and return (source_type, download_url, episode_id, title).

    source_type is "youtube" or "rss".
    """
    # Direct YouTube video
    if _is_youtube(source) and not _is_playlist_or_channel(source):
        video_id = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", source)
        if video_id:
            vid = video_id.group(1)
            return "youtube", source, vid, vid
        # fall through to yt-dlp resolution

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
# Transcript caching
# ═════════════════════════════════════════════════════════════════════════════

def load_or_transcribe(mp3_path: str, episode_id: str, skip_cache: bool = False) -> list[dict]:
    """Return transcript segments, using cache when available."""
    cache_path = _TRANSCRIPT_CACHE / f"{episode_id}.json"

    if not skip_cache and cache_path.exists():
        print(f"  Using cached transcript ({cache_path.name})")
        with open(cache_path) as f:
            return json.load(f)

    print("  Transcribing with AWS Transcribe... (this takes a few minutes)")
    segments = transcribe_audio(mp3_path, episode_id)
    with open(cache_path, "w") as f:
        json.dump(segments, f, indent=2)
    print(f"  Transcript cached → {cache_path.name} ({len(segments)} segments)")
    return segments


# ═════════════════════════════════════════════════════════════════════════════
# Core pipeline pass
# ═════════════════════════════════════════════════════════════════════════════

def _fmt(secs: float) -> str:
    """Format seconds as MM:SS."""
    m, s = divmod(int(secs), 60)
    return f"{m:02d}:{s:02d}"


def run_detection_and_cut(
    original_mp3: str,
    segments: list[dict],
    ad_segs: list[dict],
    out_dir: str,
    pass_num: int,
    snap: bool,
) -> tuple[str, list[dict]]:
    """Snap boundaries, cut from original, return (cleaned_path, final_segs)."""
    final_segs = snap_ad_boundaries(ad_segs, original_mp3) if snap else ad_segs

    out_path = os.path.join(out_dir, f"cleaned_v{pass_num}.mp3")
    splice_audio(original_mp3, final_segs, out_path)
    return out_path, final_segs


# ═════════════════════════════════════════════════════════════════════════════
# Listening guide
# ═════════════════════════════════════════════════════════════════════════════

def print_listening_guide(ad_segs: list[dict], transcript: list[dict]) -> None:
    """Print where to scrub in the cleaned file to verify each cut."""
    W = 70
    print()
    print("═" * W)
    print("  LISTENING GUIDE  —  where to verify each cut")
    print("═" * W)
    print()
    # Compute playback positions in the cleaned file
    cumulative_removed = 0.0
    for i, seg in enumerate(ad_segs, 1):
        duration = seg["end"] - seg["start"]
        cleaned_cut_start = seg["start"] - cumulative_removed

        # Transcript snippet covering the cut
        covered = " ".join(
            s["text"] for s in transcript
            if s["end"] >= seg["start"] - 2 and s["start"] <= seg["end"] + 2
        )
        snippet = (covered[:120] + "…") if len(covered) > 120 else covered

        print(f"  Cut #{i}:  {_fmt(seg['start'])} → {_fmt(seg['end'])}  ({duration:.0f}s removed)")
        print(f"    Content: \"{snippet}\"")
        print(f"    ➜ In the cleaned file, scrub to {_fmt(max(0, cleaned_cut_start - 3))} "
              f"and listen through the join")
        print()
        cumulative_removed += duration
    print("═" * W)


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the ad cleaner on one episode end-to-end",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("source",
        help="YouTube URL / @handle / playlist ID / RSS URL / podcast name")
    parser.add_argument("--max-iter", type=int, default=2,
        help="Max retry iterations if residuals found (default: 2)")
    parser.add_argument("--skip-transcribe", action="store_true",
        help="Reuse cached transcript from eval/transcripts/ if available")
    parser.add_argument("--no-snap", action="store_true",
        help="Disable silence-boundary snapping of cut points")
    parser.add_argument("--out-dir", default="test_output",
        help="Output directory (default: ./test_output)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    snap = not args.no_snap

    W = 70
    print()
    print("═" * W)
    print("  AD CLEANER TEST HARNESS")
    print("═" * W)
    print()

    # ── Step 1: Resolve source ────────────────────────────────────────────────
    print("[1/5] Resolving source...")
    source_type, download_url, episode_id, title = resolve_source(args.source)
    print(f"  Episode : {title}")
    print(f"  ID      : {episode_id}")
    print(f"  Type    : {source_type}")

    # ── Step 2: Download ──────────────────────────────────────────────────────
    print()
    print("[2/5] Downloading episode...")
    original_mp3 = download_episode(source_type, download_url, episode_id, str(out_dir))
    size_mb = os.path.getsize(original_mp3) / 1_048_576
    print(f"  Saved → {original_mp3}  ({size_mb:.1f} MB)")

    # ── Step 3: Transcribe ────────────────────────────────────────────────────
    print()
    print("[3/5] Transcribing...")
    segments = load_or_transcribe(original_mp3, episode_id, skip_cache=args.skip_transcribe)
    if not segments:
        print("  ERROR: Transcript is empty — cannot detect ads.")
        sys.exit(1)

    # ── Step 4: Detect + cut ──────────────────────────────────────────────────
    print()
    print("[4/5] Detecting ads...")
    ad_segs = detect_ads(segments)
    if not ad_segs:
        print()
        print("  NO ADS DETECTED — nothing to remove.")
        print(f"  Original file: {original_mp3}")
        sys.exit(0)

    total_ad_secs = sum(s["end"] - s["start"] for s in ad_segs)
    print(f"  Detected {len(ad_segs)} ad segment(s) — {total_ad_secs:.0f}s total")
    for i, s in enumerate(ad_segs, 1):
        print(f"    #{i}: {_fmt(s['start'])} → {_fmt(s['end'])}  ({s['end']-s['start']:.0f}s)")

    print()
    print(f"  Removing ads (snap to silence: {'on' if snap else 'off'})...")
    pass_num = 1
    cleaned_mp3, final_segs = run_detection_and_cut(
        original_mp3, segments, ad_segs, str(out_dir), pass_num, snap
    )
    print(f"  → Cleaned file v{pass_num}: {cleaned_mp3}")

    # ── Step 5: Verify + retry loop ───────────────────────────────────────────
    print()
    print("[5/5] Verifying (re-transcribing cleaned file)...")

    for iteration in range(args.max_iter):
        clean_id = f"{episode_id}_clean_v{pass_num}"
        clean_segments = transcribe_audio(cleaned_mp3, clean_id)
        residuals = detect_ads(clean_segments)

        if not residuals:
            print(f"  ✓ CLEAN — no residual ads found (pass {pass_num})")
            break

        residual_secs = sum(r["end"] - r["start"] for r in residuals)
        print(f"  ✗ Residuals found: {len(residuals)} segment(s), {residual_secs:.0f}s")
        for r in residuals:
            print(f"    [{_fmt(r['start'])} → {_fmt(r['end'])}] in cleaned file")

        if iteration + 1 >= args.max_iter:
            print(f"  Reached max-iter ({args.max_iter}) — stopping with residuals present.")
            break

        # Translate residuals back to original-file coordinates, then re-cut original
        print(f"  Re-cutting from original with {len(final_segs) + len(residuals)} merged segments...")
        translated = [
            {
                "start": _translate_cleaned_to_original(r["start"], final_segs),
                "end":   _translate_cleaned_to_original(r["end"],   final_segs),
            }
            for r in residuals
        ]
        merged = _merge_overlapping_ads(final_segs + translated)
        pass_num += 1
        cleaned_mp3, final_segs = run_detection_and_cut(
            original_mp3, segments, merged, str(out_dir), pass_num, snap
        )
        print(f"  → Cleaned file v{pass_num}: {cleaned_mp3}")

    # ── Summary ───────────────────────────────────────────────────────────────
    total_removed = sum(s["end"] - s["start"] for s in final_segs)
    print()
    print("═" * W)
    print(f"  DONE")
    print(f"  Episode    : {title}")
    print(f"  Removed    : {len(final_segs)} segment(s), {total_removed:.0f}s total")
    print(f"  Output     : {cleaned_mp3}")
    print(f"  Passes     : {pass_num}")
    print(f"  Transcript : eval/transcripts/{episode_id}.json  (cached)")
    print("═" * W)

    print_listening_guide(final_segs, segments)


if __name__ == "__main__":
    main()
