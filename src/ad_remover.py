"""Ad removal pipeline for downloaded podcast audio.

Pipeline:
    1. Upload the audio file to a temporary S3 prefix and transcribe it with
       AWS Transcribe (word-level timestamps).
    2. Send the transcript to AWS Bedrock (us.anthropic.claude-sonnet-4-6 via the
       Converse API) to identify ad-segment timestamps as JSON.
    3. Use ffmpeg to splice out the ad segments and stitch the remaining audio
       into a single output MP3.
    4. Fall back to the original file if any step fails.

Environment variables (all optional — sensible defaults provided):
    REMOVE_ADS              – Set to "false" to skip ad removal entirely (default: "true").
    S3_BUCKET               – Bucket used for the temporary Transcribe input file
                              (re-uses the existing podcast bucket).
    AWS_DEFAULT_REGION      – AWS region for Transcribe and Bedrock clients.
    TRANSCRIBE_LANGUAGE_CODE – BCP-47 language code passed to Transcribe (default: "en-US").
    BEDROCK_MODEL_ID        – Bedrock model ID for ad-segment verification (second-pass
                              confirmation of long segments).  Defaults to Claude Sonnet for
                              accuracy (default: "us.anthropic.claude-sonnet-4-6").
    BEDROCK_DETECT_MODEL_ID – Bedrock model ID for first-pass ad detection across all chunks.
                              Defaults to BEDROCK_MODEL_ID when not set.  Set to a cheaper
                              model (e.g. Claude Haiku) to reduce per-episode detection cost
                              while keeping Sonnet for the more critical verification step.
    TRANSCRIBE_POLL_INTERVAL – Seconds between Transcribe job status polls (default: 10).
    TRANSCRIBE_MAX_WAIT     – Maximum seconds to wait for a Transcribe job (default: 3600).
    REMOVE_ADS_DRY_RUN      – Set to "true" to detect ads and log them without actually
                              splicing the audio file (default: "false").  Useful for
                              evaluating detection quality before enabling full removal.
    MAX_AD_SEGMENT_SECS     – Ad segments longer than this are treated as false positives and
                              skipped (default: "180").  Legitimate podcast ads rarely exceed
                              3 minutes; very long segments usually indicate content misclassified
                              as an ad.
    AD_VERIFY_THRESHOLD_SECS – Segments longer than this value trigger a second Bedrock call to
                              confirm they are genuinely ads before removal (default: "90").
                              Set to "0" to verify every segment, or a very large number to
                              disable verification.
    AD_SNAP_TO_SILENCE      – Set to "true" to snap ad-segment boundaries to the nearest silence
                              gap (±3 s window), producing cleaner audio cuts (default: "true").
    TRANSCRIBE_CACHE_ENABLED – Set to "false" to disable S3 transcript caching (default: "true").
                               When enabled, the segment JSON from each successful Transcribe job is
                               saved to S3 at ``transcribe-cache/{slug}/{video_id}.json`` and reused on
                               subsequent runs, eliminating repeated transcription costs for
                               reprocessed episodes.
    TRANSCRIBE_CACHE_PREFIX  – S3 key prefix for cached transcripts (default: "transcribe-cache").
    TRIM_MUSIC_INTRO        – Set to "true" to trim non-speech audio before the first
                              transcript word (default: "false").  Per-feed config
                              trim_music_intro=true overrides this.
    TRIM_MUSIC_OUTRO        – Set to "true" to trim non-speech audio after the last
                              transcript word (default: "false").
    MUSIC_INTRO_MIN_SECS    – Minimum intro gap in seconds to treat as music (default: "8.0").
    MUSIC_OUTRO_MIN_SECS    – Minimum outro gap in seconds to treat as music (default: "5.0").
    SPLICE_LOUDNORM         – Set to "false" to disable EBU R128 loudness normalisation after
                              splicing (default: "true").  Loudnorm equalises loudness across
                              all kept intervals so volume discontinuities at cut points are
                              inaudible.  Adds ~10-20% to ffmpeg processing time.
    AD_TRANSCRIBE_WINDOWS   – Comma-separated list of ``start:end`` time ranges (in seconds)
                              to transcribe, instead of the full audio file.  Either bound can
                              use the token ``end`` (meaning the total duration) or ``end-N``
                              (meaning total duration minus N seconds).  Example::

                                  AD_TRANSCRIBE_WINDOWS=0:300,end-600:end

                              transcribes only the first 5 minutes and the last 10 minutes —
                              useful for podcasts where ads only appear near the beginning and
                              end.  When not set (default) the entire file is transcribed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import ssl
import subprocess
import time
import uuid

import boto3
import certifi

from utils import env_float, env_int, retry_aws_call

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Internal type alias
# ---------------------------------------------------------------------------
AdSegment = dict  # {"start": float, "end": float}

#: Sentinel strings returned by :func:`remove_ads` in the summary position when
#: a pipeline stage fails.  Callers **must** exclude these before writing the
#: value to the episode manifest or an RSS feed — they are error signals, not
#: human-readable content.
REMOVE_ADS_ERROR_CODES: frozenset[str] = frozenset(
    {
        "TRANSCRIBE_FAILED",
        "DETECT_FAILED",
        "SPLICE_FAILED",
    }
)


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe timeouts
# ---------------------------------------------------------------------------
#
# Every ffmpeg and ffprobe call must be bounded.  A hung child process holds the
# S3 distributed lock (TTL 3600s, see distributed_lock.py) for as long as it
# lives, which silently blocks every subsequent cron run -- the pipeline just
# stops producing episodes with no error reported anywhere.  A timeout raises
# TimeoutExpired, which the splice paths convert into a splice failure so the
# existing retry logic picks the episode up on a later run.


def _ffmpeg_timeout(name: str, default: float) -> float:
    """Return the configured timeout in seconds for an ffmpeg/ffprobe stage."""
    return env_float(name, default)


# ---------------------------------------------------------------------------
# Fix #5 – Silence detection + boundary snapping
# ---------------------------------------------------------------------------


def detect_silence(
    mp3_path: str,
    noise_threshold: str = "-35dB",
    min_duration: float = 0.5,
) -> list[dict]:
    """Detect silence intervals in *mp3_path* using ffmpeg silencedetect.

    Args:
        mp3_path:        Path to the audio file.
        noise_threshold: Noise floor passed to silencedetect (e.g. ``"-35dB"``).
        min_duration:    Minimum silence duration in seconds to report.

    Returns:
        List of ``{"start": float, "end": float, "duration": float}`` dicts,
        sorted by start time.  Returns an empty list if ffmpeg is unavailable
        or the file contains no qualifying silences.
    """
    cmd = [
        "ffmpeg",
        "-i",
        mp3_path,
        "-af",
        f"silencedetect=noise={noise_threshold}:d={min_duration}",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_ffmpeg_timeout("FFMPEG_SILENCEDETECT_TIMEOUT_SECS", 1800.0),
        )
    except FileNotFoundError:
        logger.warning("[AdRemover] ffmpeg not found — silence detection skipped")
        return []
    except subprocess.TimeoutExpired:
        # Silence detection only refines cut boundaries, so a timeout degrades
        # to "no silences found" rather than failing the episode.
        logger.warning("[AdRemover] ffmpeg silencedetect timed out for %s — skipping silence snapping", mp3_path)
        return []

    silences: list[dict] = []
    current_start: float | None = None
    for line in result.stderr.split("\n"):
        if "silence_start:" in line:
            try:
                current_start = float(line.split("silence_start:")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif "silence_end:" in line and current_start is not None:
            try:
                parts = line.split("silence_end:")[1].strip().split("|")
                end = float(parts[0].strip().split()[0])
                duration = (
                    float(parts[1].split("silence_duration:")[1].strip()) if len(parts) > 1 else end - current_start
                )
                silences.append(
                    {
                        "start": round(current_start, 2),
                        "end": round(end, 2),
                        "duration": round(duration, 2),
                    }
                )
                current_start = None
            except (ValueError, IndexError):
                current_start = None

    return silences


def _snap_to_silence_boundary(
    time: float,
    silences: list[dict],
    window: float = 3.0,
    prefer_earlier: bool = False,
) -> float:
    """Return the silence boundary nearest to *time* within ±*window* seconds.

    If no silence boundary falls within the window, *time* is returned unchanged.

    Args:
        time:           Timestamp to snap (seconds).
        silences:       Silence interval list from :func:`detect_silence`.
        window:         Max distance (seconds) to move the boundary.
        prefer_earlier: When ``True``, break ties in favour of earlier candidates.
                        Use for segment **starts** — snapping earlier avoids cutting
                        into the content just before the ad begins.
                        When ``False`` (default), ties favour later candidates,
                        which is correct for segment **ends** — snapping later avoids
                        clipping the ad outro.

    Returns:
        Nearest silence boundary within *window*, or *time* if none found.
    """
    best_time = time
    best_dist = float("inf")
    for silence in silences:
        for candidate in (silence["start"], silence["end"]):
            dist = abs(candidate - time)
            if dist > window:
                continue
            if dist < best_dist:
                best_dist = dist
                best_time = candidate
            elif dist == best_dist:
                if (prefer_earlier and candidate < best_time) or (not prefer_earlier and candidate > best_time):
                    best_time = candidate
    return best_time


def snap_ad_boundaries(
    ad_segments: list[AdSegment],
    mp3_path: str,
    snap_window: float = 3.0,
    silences: list[dict] | None = None,
) -> list[AdSegment]:
    """Snap each ad-segment boundary to the nearest silence gap.

    Cuts landing on silence rather than mid-word produce much cleaner audio.
    Falls back to the original boundaries if silence detection fails or finds
    nothing within *snap_window* seconds.

    Args:
        ad_segments:  Candidate ad segments to adjust.
        mp3_path:     Source audio file (used to detect silences).
        snap_window:  Maximum seconds to move a boundary (default: 3.0).
        silences:     Pre-computed silence intervals (avoids redundant ffmpeg call).

    Returns:
        Adjusted segment list.  Any segment shrunk below ``_MIN_AD_SECONDS``
        after snapping is kept at its original boundaries.
    """
    if not ad_segments:
        return ad_segments

    if silences is None:
        try:
            silences = detect_silence(mp3_path)
        except Exception as exc:
            logger.warning("[AdRemover] Silence detection failed — keeping original boundaries: %s", exc)
            return ad_segments

    if not silences:
        logger.debug("[AdRemover] No silence intervals found — skipping boundary snapping")
        return ad_segments

    _MIN_AD = 5.0
    snapped: list[AdSegment] = []
    for seg in ad_segments:
        # Snap start earlier (prefer_earlier=True): avoids cutting into content before ad
        # Snap end later (prefer_earlier=False): avoids clipping the ad outro
        new_start = _snap_to_silence_boundary(seg["start"], silences, snap_window, prefer_earlier=True)
        new_end = _snap_to_silence_boundary(seg["end"], silences, snap_window, prefer_earlier=False)
        if new_end - new_start < _MIN_AD:
            logger.warning(
                "[AdRemover] Silence snap would shrink [%.1f–%.1f] to %.1fs — keeping original",
                seg["start"],
                seg["end"],
                new_end - new_start,
            )
            snapped.append(seg)
        else:
            if new_start != seg["start"] or new_end != seg["end"]:
                logger.info(
                    "[AdRemover] Snapped [%.1f–%.1f] → [%.1f–%.1f]",
                    seg["start"],
                    seg["end"],
                    new_start,
                    new_end,
                )
            snapped.append({"start": new_start, "end": new_end})

    return snapped


# ---------------------------------------------------------------------------
# Music intro/outro detection
# ---------------------------------------------------------------------------


def detect_music_bookends(
    segments: list[dict],
    mp3_path: str,
    min_intro_secs: float = 8.0,
    min_outro_secs: float = 5.0,
    silences: list[dict] | None = None,
) -> list[AdSegment]:
    """Detect music intro/outro by finding non-silent audio outside transcript boundaries.

    The region [0, first_word_start] is a music intro if its duration >= min_intro_secs
    and it is not entirely silent.  The region [last_word_end, audio_duration] is a music
    outro by the same criteria using min_outro_secs.

    Returns:
        List of AdSegment dicts with an extra "label" key ("music_intro"/"music_outro").
        Empty list if segments is empty or audio duration cannot be read.
    """
    if not segments:
        return []

    duration = _get_audio_duration(mp3_path)
    if duration <= 0.0:
        logger.warning("[AdRemover] Could not read audio duration for music detection: %s", mp3_path)
        return []

    first_speech = segments[0]["start"]
    last_speech = segments[-1]["end"]
    results: list[AdSegment] = []

    if silences is None:
        silences = detect_silence(mp3_path)

    def _region_has_audio(region_start: float, region_end: float) -> bool:
        """Return True if [region_start, region_end] contains non-silent audio."""
        region_dur = region_end - region_start
        if region_dur <= 0:
            return False
        silence_secs = 0.0
        for s in silences:
            overlap_start = max(s["start"], region_start)
            overlap_end = min(s["end"], region_end)
            if overlap_end > overlap_start:
                silence_secs += overlap_end - overlap_start
        return (silence_secs / region_dur) < 0.85

    # Intro
    if first_speech >= min_intro_secs and _region_has_audio(0.0, first_speech):
        logger.info("[AdRemover] Music intro detected: [0.0, %.2fs] (%.1fs)", first_speech, first_speech)
        results.append({"start": 0.0, "end": round(first_speech, 2), "label": "music_intro"})

    # Outro
    outro_dur = duration - last_speech
    if outro_dur >= min_outro_secs and _region_has_audio(last_speech, duration):
        logger.info(
            "[AdRemover] Music outro detected: [%.2fs, %.2fs] (%.1fs)",
            last_speech,
            duration,
            outro_dur,
        )
        results.append({"start": round(last_speech, 2), "end": round(duration, 2), "label": "music_outro"})

    return results


# ---------------------------------------------------------------------------
# Step 1 – Transcription (AWS Transcribe)
# ---------------------------------------------------------------------------


def _safe_namespace(namespace: str) -> str:
    """Return *namespace* reduced to characters that are safe in an S3 key."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", namespace).strip("-.")
    return cleaned[:80]


def _cache_key(video_id: str, suffix: str, namespace: str = "") -> str:
    """Return the S3 cache key for *video_id* under *namespace*.

    Episode identifiers are only unique *within* a feed.  RSS ``<guid>`` values
    are frequently bare integers, so ``episode_id_from_guid`` produces ids like
    ``"1"`` or ``"12345"`` that collide across podcasts.  Without a namespace,
    two different episodes shared one transcript and one ad-segment cache entry,
    and the second podcast had another show's ad timestamps spliced out of it.

    Args:
        video_id:  Episode identifier (unique within the namespace).
        suffix:    Key suffix, e.g. ``".json"``, ``"_ads.json"``.
        namespace: Per-podcast namespace, usually the S3 slug.  Empty means the
            legacy flat layout.

    Returns:
        The S3 object key.
    """
    prefix = os.environ.get("TRANSCRIBE_CACHE_PREFIX", "transcribe-cache")
    ns = _safe_namespace(namespace)
    if ns:
        return f"{prefix}/{ns}/{video_id}{suffix}"
    return f"{prefix}/{video_id}{suffix}"


def _cache_read_keys(video_id: str, suffix: str, namespace: str = "") -> list[str]:
    """Return the keys to try when *reading* a cache entry, best match first.

    The flat (un-namespaced) key is included as a fallback so entries written
    before namespacing are still honoured instead of forcing a re-transcription
    of the whole back catalogue.
    """
    keys = [_cache_key(video_id, suffix, namespace)]
    flat = _cache_key(video_id, suffix, "")
    if flat != keys[0]:
        keys.append(flat)
    return keys


def _transcript_cache_key(video_id: str, namespace: str = "") -> str:
    """Return the S3 key used for caching a transcript."""
    return _cache_key(video_id, ".json", namespace)


def _get_cached_bytes(s3_client, bucket: str, keys: list[str], label: str, video_id: str) -> bytes | None:
    """Return the body of the first key in *keys* that exists, else ``None``.

    Args:
        s3_client: Boto3 S3 client.
        bucket:    S3 bucket name.
        keys:      Candidate keys, best match first.
        label:     Human-readable cache name, used only for log messages.
        video_id:  Episode identifier, used only for log messages.
    """
    from botocore.exceptions import ClientError

    for key in keys:
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
            return resp["Body"].read()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("NoSuchKey", "404"):
                logger.debug("[AdRemover] %s cache error for %s (%s): %s", label, video_id, code, exc)
        except Exception as exc:
            logger.debug("[AdRemover] %s cache load failed for %s: %s", label, video_id, exc)
    logger.debug("[AdRemover] %s cache MISS for %s", label, video_id)
    return None


def _load_transcript_cache(s3_client, bucket: str, video_id: str, namespace: str = "") -> list[dict] | None:
    """Try to load a cached transcript from S3.

    Args:
        s3_client: Boto3 S3 client.
        bucket:    S3 bucket name.
        video_id:  Episode identifier used as the cache key.
        namespace: Per-podcast cache namespace (see :func:`_cache_key`).

    Returns:
        Cached segment list, or ``None`` if not found or loading fails.
    """
    raw = _get_cached_bytes(
        s3_client, bucket, _cache_read_keys(video_id, ".json", namespace), "Transcript", video_id
    )
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.debug("[AdRemover] Transcript cache decode failed for %s: %s", video_id, exc)
        return None
    if isinstance(data, list):
        logger.info(
            "[AdRemover] Transcript cache HIT for %s (%d segments) — skipping Transcribe job",
            video_id,
            len(data),
        )
        return data
    logger.warning("[AdRemover] Cached transcript for %s is not a list — ignoring", video_id)
    return None


def _save_transcript_cache(
    s3_client, bucket: str, video_id: str, segments: list[dict], namespace: str = ""
) -> None:
    """Persist a transcript segment list to S3 for future reuse.

    Args:
        s3_client: Boto3 S3 client.
        bucket:    S3 bucket name.
        video_id:  Episode identifier used as the cache key.
        segments:  Segment list returned by ``_items_to_segments``.
        namespace: Per-podcast cache namespace (see :func:`_cache_key`).
    """
    key = _cache_key(video_id, ".json", namespace)
    try:
        body = json.dumps(segments).encode("utf-8")
        s3_client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
        logger.debug("[AdRemover] Transcript cache saved for %s → s3://%s/%s", video_id, bucket, key)
    except Exception as exc:
        logger.warning("[AdRemover] Could not save transcript cache for %s: %s", video_id, exc)


def _load_summary_cache(s3_client, bucket: str, video_id: str, namespace: str = "") -> str | None:
    """Try to load a cached episode summary from S3."""
    raw = _get_cached_bytes(
        s3_client, bucket, _cache_read_keys(video_id, "_summary.txt", namespace), "Summary", video_id
    )
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except Exception as exc:
        logger.debug("[AdRemover] Summary cache decode failed for %s: %s", video_id, exc)
        return None
    if text:
        logger.info("[AdRemover] Summary cache HIT for %s", video_id)
        return text
    return None


def _save_summary_cache(s3_client, bucket: str, video_id: str, summary: str, namespace: str = "") -> None:
    """Persist an episode summary to S3."""
    key = _cache_key(video_id, "_summary.txt", namespace)
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=summary.encode("utf-8"),
            ContentType="text/plain",
        )
        logger.debug("[AdRemover] Summary cache saved for %s", video_id)
    except Exception as exc:
        logger.warning("[AdRemover] Could not save summary cache for %s: %s", video_id, exc)


def _save_transcript_text(
    s3_client, bucket: str, video_id: str, segments: list[dict], namespace: str = ""
) -> None:
    """Persist the full transcript as plain text to S3 alongside the segment cache."""
    key = _cache_key(video_id, ".txt", namespace)
    text = "\n".join(f"[{s['start']:.1f}s]  {s['text']}" for s in segments)
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/plain",
        )
        logger.info("[AdRemover] Transcript text saved to s3://%s/%s", bucket, key)
    except Exception as exc:
        logger.warning("[AdRemover] Could not save transcript text for %s: %s", video_id, exc)


def _load_ad_segments_cache(s3_client, bucket: str, video_id: str, namespace: str = "") -> list[AdSegment] | None:
    """Try to load cached detected ad-segments from S3.

    Stored alongside the transcript cache at
    ``transcribe-cache/{namespace}/{video_id}_ads.json``.  Returns ``None`` on
    miss or error so the caller falls back to a real Bedrock call.
    """
    raw = _get_cached_bytes(
        s3_client, bucket, _cache_read_keys(video_id, "_ads.json", namespace), "Ad-segments", video_id
    )
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.debug("[AdRemover] Ad-segments cache decode failed for %s: %s", video_id, exc)
        return None
    if isinstance(data, list):
        logger.info(
            "[AdRemover] Ad-segments cache HIT for %s (%d segments) — skipping Bedrock detection",
            video_id,
            len(data),
        )
        return data
    return None


def _save_ad_segments_cache(
    s3_client, bucket: str, video_id: str, ad_segments: list[AdSegment], namespace: str = ""
) -> None:
    """Persist detected ad-segments to S3 for future reuse.

    Empty results are intentionally not cached: a cache miss is always safer
    than permanently treating a false-negative detection as confirmed "no ads".
    On the next run, Bedrock will be called again and given another chance to
    detect ads with the latest prompt and model.
    """
    if not ad_segments:
        logger.debug(
            "[AdRemover] Ad-segments cache skipped for %s (empty result — not persisting to avoid false-negative lock-in)",
            video_id,
        )
        return
    key = _cache_key(video_id, "_ads.json", namespace)
    try:
        body = json.dumps(ad_segments).encode("utf-8")
        s3_client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
        logger.debug("[AdRemover] Ad-segments cache saved for %s", video_id)
    except Exception as exc:
        logger.warning("[AdRemover] Could not save ad-segments cache for %s: %s", video_id, exc)


# ---------------------------------------------------------------------------
# Fix #5 – Selective transcription windows (AD_TRANSCRIBE_WINDOWS)
# ---------------------------------------------------------------------------


def _get_audio_duration(mp3_path: str) -> float:
    """Return the duration of *mp3_path* in seconds using ffprobe.

    Returns ``0.0`` if ffprobe fails so callers can fall back gracefully.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                mp3_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(result.stdout.strip())
    except Exception as exc:
        logger.warning("[AdRemover] ffprobe duration check failed: %s", exc)
        return 0.0


def _parse_transcribe_windows(raw: str, duration: float) -> list[tuple[float, float]]:
    """Parse ``AD_TRANSCRIBE_WINDOWS`` into a list of ``(start_sec, end_sec)`` tuples.

    Format: comma-separated ranges, each ``start:end`` where either value may be
    ``end`` (meaning *duration*) or ``end-N`` (meaning *duration − N*).

    Example::

        "0:300,end-600:end"   → [(0.0, 300.0), (duration-600, duration)]

    Invalid ranges are skipped with a warning.  Returns an empty list when
    *raw* is empty/blank — the caller treats this as "transcribe everything".

    Args:
        raw:      The raw env-var string.
        duration: Total audio duration in seconds (used to resolve ``end`` tokens).

    Returns:
        List of ``(start, end)`` tuples, clamped to ``[0, duration]``.
    """
    if not raw or not raw.strip():
        return []

    windows: list[tuple[float, float]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            logger.warning("[AdRemover] Ignoring invalid AD_TRANSCRIBE_WINDOWS entry: %r", part)
            continue
        start_raw, end_raw = part.split(":", 1)
        try:

            def _resolve(token: str, total: float) -> float:
                token = token.strip().lower()
                if token == "end":
                    return total
                if token.startswith("end-"):
                    return max(0.0, total - float(token[4:]))
                if token.startswith("end+"):
                    return min(total, total + float(token[4:]))
                return float(token)

            start = max(0.0, _resolve(start_raw, duration))
            end = min(duration, _resolve(end_raw, duration)) if duration > 0 else _resolve(end_raw, duration)
            if end <= start:
                logger.warning(
                    "[AdRemover] Skipping degenerate window %r (start=%.1f >= end=%.1f)",
                    part,
                    start,
                    end,
                )
                continue
            windows.append((start, end))
        except ValueError as exc:
            logger.warning("[AdRemover] Ignoring unparseable AD_TRANSCRIBE_WINDOWS entry %r: %s", part, exc)

    return windows


def _extract_audio_window(mp3_path: str, start: float, end: float, out_path: str) -> None:
    """Extract a sub-clip ``[start, end]`` seconds from *mp3_path* into *out_path*.

    Uses ``ffmpeg -ss``/``-to`` with ``-c copy`` for fast stream copy when
    possible, falling back to re-encode if the source is variable-bitrate.

    Args:
        mp3_path: Source audio file.
        start:    Clip start in seconds.
        end:      Clip end in seconds.
        out_path: Destination path for the extracted clip.

    Raises:
        RuntimeError: If ffmpeg exits with a non-zero return code.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-to",
        str(end),
        "-i",
        mp3_path,
        "-c",
        "copy",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg window extract failed (rc={result.returncode}): {result.stderr[-500:]}")


def transcribe_audio(mp3_path: str, video_id: str, cache_namespace: str = "") -> list[dict]:
    """Upload *mp3_path* to S3 and transcribe it with AWS Transcribe.

    Returns a list of segment dicts, each with keys ``start`` (float),
    ``end`` (float), and ``text`` (str) derived from the word-level items
    in the Transcribe output.

    Args:
        mp3_path: Local path to the audio file.
        video_id: Episode identifier, used to name the temporary S3 object and
                  the Transcribe job.

    Returns:
        List of segment dicts.  Empty list when Transcribe produces no output.

    Raises:
        RuntimeError: On any AWS API error or if the Transcribe job fails.
    """
    from botocore.exceptions import ClientError

    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        raise RuntimeError("S3_BUCKET must be set to use AWS Transcribe")

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    language_code = os.environ.get("TRANSCRIBE_LANGUAGE_CODE", "en-US")
    poll_interval = env_int("TRANSCRIBE_POLL_INTERVAL", 10)
    max_wait = env_int("TRANSCRIBE_MAX_WAIT", 3600)
    cache_enabled = os.environ.get("TRANSCRIBE_CACHE_ENABLED", "true").lower() not in ("false", "0", "no")
    # Skip caching for evaluator re-transcriptions (eval- prefix) — those target the cleaned
    # file and should not overwrite the original episode's cache entry.
    use_cache = cache_enabled and not video_id.startswith("eval-")

    s3_client = boto3.client("s3", region_name=region)
    transcribe_client = boto3.client("transcribe", region_name=region)

    # 0. Check transcript cache (skip expensive Transcribe job if already done)
    if use_cache:
        cached = _load_transcript_cache(s3_client, bucket, video_id, cache_namespace)
        if cached is not None:
            return cached

    # 1a. Selective transcription windows (AD_TRANSCRIBE_WINDOWS)
    #     If set, only transcribe specified sub-clips to reduce Transcribe cost.
    windows_raw = os.environ.get("AD_TRANSCRIBE_WINDOWS", "").strip()
    if windows_raw:
        duration = _get_audio_duration(mp3_path)
        windows = _parse_transcribe_windows(windows_raw, duration)
        if windows:
            import tempfile as _tempfile

            all_segments: list[dict] = []
            for w_idx, (w_start, w_end) in enumerate(windows):
                with _tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as w_tmp:
                    w_tmp_path = w_tmp.name
                try:
                    _extract_audio_window(mp3_path, w_start, w_end, w_tmp_path)
                    w_id = f"{video_id}-w{w_idx}"
                    w_key = f"transcribe-tmp/{w_id}.mp3"
                    logger.info(
                        "[AdRemover] Window %d/%d: uploading %.1f-%.1fs sub-clip for transcription",
                        w_idx + 1,
                        len(windows),
                        w_start,
                        w_end,
                    )
                    retry_aws_call(
                        lambda k=w_key, p=w_tmp_path: s3_client.upload_file(p, bucket, k),
                        label=f"s3.upload_file[window-{w_idx}]",
                    )
                    w_uri = f"s3://{bucket}/{w_key}"
                    w_safe = re.sub(r"[^A-Za-z0-9_-]", "-", w_id)[:64].strip("-")
                    w_job = f"pad-{w_safe}-{uuid.uuid4().hex[:8]}"
                    retry_aws_call(
                        lambda j=w_job, u=w_uri: transcribe_client.start_transcription_job(
                            TranscriptionJobName=j,
                            Media={"MediaFileUri": u},
                            MediaFormat="mp3",
                            LanguageCode=language_code,
                            Settings={"ShowSpeakerLabels": False, "ChannelIdentification": False},
                        ),
                        label=f"transcribe.start[window-{w_idx}]",
                    )
                    # Poll until complete
                    w_elapsed = 0
                    w_status_resp: dict = {}
                    while w_elapsed < max_wait:
                        time.sleep(poll_interval)
                        w_elapsed += poll_interval
                        w_status_resp = transcribe_client.get_transcription_job(TranscriptionJobName=w_job)
                        w_status = w_status_resp["TranscriptionJob"]["TranscriptionJobStatus"]
                        if w_status == "COMPLETED":
                            break
                        if w_status == "FAILED":
                            raise RuntimeError(
                                f"Transcribe window job {w_job} failed: "
                                + w_status_resp["TranscriptionJob"].get("FailureReason", "unknown")
                            )
                    else:
                        raise RuntimeError(f"Transcribe window job {w_job} timed out after {max_wait}s")

                    w_uri_result = w_status_resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
                    import urllib.request as _req

                    with _req.urlopen(w_uri_result, context=_SSL_CTX) as r:
                        w_data = json.loads(r.read())
                    w_items = w_data.get("results", {}).get("items", [])
                    w_segs = _items_to_segments(w_items)
                    # Offset all segment timestamps by the window start
                    for seg in w_segs:
                        seg["start"] += w_start
                        seg["end"] += w_start
                    all_segments.extend(w_segs)
                    logger.info(
                        "[AdRemover] Window %d/%d complete: %d segments (offset +%.1fs)",
                        w_idx + 1,
                        len(windows),
                        len(w_segs),
                        w_start,
                    )
                finally:
                    with contextlib.suppress(OSError):
                        os.remove(w_tmp_path)
                    with contextlib.suppress(Exception):
                        s3_client.delete_object(Bucket=bucket, Key=w_key)
                    with contextlib.suppress(Exception):
                        transcribe_client.delete_transcription_job(TranscriptionJobName=w_job)

            # Sort merged segments by start time
            all_segments.sort(key=lambda s: s["start"])
            logger.info(
                "[AdRemover] Windowed transcription complete: %d total segments from %d window(s)",
                len(all_segments),
                len(windows),
            )
            if use_cache:
                _save_transcript_cache(s3_client, bucket, video_id, all_segments, cache_namespace)
                _save_transcript_text(s3_client, bucket, video_id, all_segments, cache_namespace)
            return all_segments

    # 1. Upload audio to a temporary S3 key
    # Namespaced so two feeds that reuse an episode id cannot overwrite each
    # other's in-flight Transcribe input while both jobs are running.
    _tmp_ns = _safe_namespace(cache_namespace)
    tmp_key = f"transcribe-tmp/{_tmp_ns}/{video_id}.mp3" if _tmp_ns else f"transcribe-tmp/{video_id}.mp3"
    logger.info("[AdRemover] Uploading %s to s3://%s/%s for transcription", mp3_path, bucket, tmp_key)
    retry_aws_call(
        lambda: s3_client.upload_file(mp3_path, bucket, tmp_key),
        label="s3.upload_file[transcribe-tmp]",
    )

    media_uri = f"s3://{bucket}/{tmp_key}"
    # Transcribe job names only allow [A-Za-z0-9_-] — sanitize video_id
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", video_id)[:64].strip("-")
    job_name = f"pad-{safe_id}-{uuid.uuid4().hex[:8]}"

    try:
        # 2. Start the transcription job
        logger.info("[AdRemover] Starting Transcribe job %s", job_name)
        retry_aws_call(
            lambda: transcribe_client.start_transcription_job(
                TranscriptionJobName=job_name,
                Media={"MediaFileUri": media_uri},
                MediaFormat="mp3",
                LanguageCode=language_code,
                Settings={"ShowSpeakerLabels": False, "ChannelIdentification": False},
            ),
            label="transcribe.start_transcription_job",
        )

        # 3. Poll until complete
        elapsed = 0
        consecutive_poll_errors = 0
        _MAX_POLL_ERRORS = 5
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval
            try:
                status_resp = transcribe_client.get_transcription_job(TranscriptionJobName=job_name)
                consecutive_poll_errors = 0
            except (ClientError, ConnectionError, OSError) as exc:
                consecutive_poll_errors += 1
                if consecutive_poll_errors >= _MAX_POLL_ERRORS:
                    raise RuntimeError(
                        f"Transcribe poll failed {consecutive_poll_errors} consecutive times: {exc}"
                    ) from exc
                logger.warning(
                    "[AdRemover] Transcribe poll error (attempt %d/%d): %s — retrying",
                    consecutive_poll_errors,
                    _MAX_POLL_ERRORS,
                    exc,
                )
                continue
            status = status_resp["TranscriptionJob"]["TranscriptionJobStatus"]
            logger.info("[AdRemover] Transcribe job %s status: %s (elapsed %ds)", job_name, status, elapsed)

            if status == "COMPLETED":
                break
            if status == "FAILED":
                reason = status_resp["TranscriptionJob"].get("FailureReason", "unknown")
                raise RuntimeError(f"Transcribe job {job_name} failed: {reason}")

        else:
            raise RuntimeError(f"Transcribe job {job_name} timed out after {max_wait}s")

        # 4. Download and parse the transcript JSON
        transcript_uri = status_resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
        logger.info("[AdRemover] Downloading transcript from %s", transcript_uri)

        import urllib.request

        with urllib.request.urlopen(transcript_uri, context=_SSL_CTX) as resp:
            transcript_data = json.loads(resp.read())

        items = transcript_data.get("results", {}).get("items", [])
        segments = _items_to_segments(items)
        logger.info("[AdRemover] Transcription complete: %d segments from %d items", len(segments), len(items))

        # Save to cache so subsequent runs skip the Transcribe job
        if use_cache:
            _save_transcript_cache(s3_client, bucket, video_id, segments, cache_namespace)
            _save_transcript_text(s3_client, bucket, video_id, segments, cache_namespace)

        return segments

    finally:
        # 5. Clean up: delete temp S3 object and Transcribe job
        try:
            s3_client.delete_object(Bucket=bucket, Key=tmp_key)
            logger.debug("[AdRemover] Deleted temp S3 object s3://%s/%s", bucket, tmp_key)
        except Exception as exc:
            logger.warning("[AdRemover] Could not delete temp S3 object: %s", exc)

        try:
            transcribe_client.delete_transcription_job(TranscriptionJobName=job_name)
            logger.debug("[AdRemover] Deleted Transcribe job %s", job_name)
        except Exception as exc:
            logger.warning("[AdRemover] Could not delete Transcribe job %s: %s", job_name, exc)


def _items_to_segments(items: list[dict], gap_threshold: float = 1.5) -> list[dict]:
    """Collapse Transcribe word-items into phrase segments.

    Consecutive words separated by less than *gap_threshold* seconds are
    grouped into a single segment.

    Args:
        items:         Raw ``items`` list from the Transcribe JSON response.
        gap_threshold: Maximum silence (seconds) allowed within a segment.

    Returns:
        List of ``{"start": float, "end": float, "text": str}`` dicts.
    """
    segments: list[dict] = []
    current_words: list[str] = []
    current_start: float | None = None
    current_end: float = 0.0

    for item in items:
        if item.get("type") != "pronunciation":
            # Punctuation items have no timing — append to current text
            if current_words and item.get("alternatives"):
                current_words.append(item["alternatives"][0].get("content", ""))
            continue

        alts = item.get("alternatives", [])
        if not alts:
            continue

        word = alts[0].get("content", "")
        start = float(item.get("start_time", 0))
        end = float(item.get("end_time", 0))

        if current_start is None:
            current_start = start
            current_end = end
            current_words = [word]
        elif start - current_end > gap_threshold:
            # Flush the current segment
            segments.append(
                {
                    "start": current_start,
                    "end": current_end,
                    "text": " ".join(current_words),
                }
            )
            current_start = start
            current_end = end
            current_words = [word]
        else:
            current_words.append(word)
            current_end = end

    if current_words and current_start is not None:
        segments.append(
            {
                "start": current_start,
                "end": current_end,
                "text": " ".join(current_words),
            }
        )

    return segments


# ---------------------------------------------------------------------------
# Step 2 – Ad detection (AWS Bedrock)
# ---------------------------------------------------------------------------

_AD_DETECTION_PROMPT = """You are an expert podcast audio editor specialising in ad removal.

Below is the word-level transcript of a podcast episode. Each line has the
format:  [start_seconds - end_seconds]  text

## Your task
Identify EVERY advertisement and sponsored segment, including host-read ads
where the host personally delivers the ad copy in their own voice.

## Common ad signals to look for
- Sponsor introductions: "brought to you by", "this episode is sponsored by",
  "our sponsor", "a word from our sponsor", "thanks to X for supporting us",
  "partnered with", "presented by"
- Discount / promo language: "use code", "promo code", "get X% off",
  "first month free", "free trial", "limited time offer", "sign up today",
  "visit X dot com slash"
- URL / website mentions in a promotional context: "dot com slash podcast",
  website names followed by offers or calls to action
- Price mentions: "for just $X", "starting at", "plans from"
- Ad outros / transitions back to content: "now back to", "and we're back",
  "alright let's get into it", "back to the show", "let's continue"

## What is NOT an ad
Do NOT flag these as ads:
- Calls to subscribe, follow, or leave a review ("subscribe to the podcast",
  "five-star review", "follow us on Twitter/X", "hit the like button")
- Mentions of the host's own book, course, or newsletter without external
  sponsor language or discount codes
- Casual organic product mentions without promotion ("I use Notion for this",
  "I love my AeroPress")
- News discussion, editorial commentary, or analysis about a company or brand
- Introductions or transitions between topics, guests, or segments
{hints_section}
## Rules
1. Only flag segments where you have clear evidence of advertising. Do NOT flag
   editorial discussion, interview content, or product mentions unless they are
   clearly promotional with a call-to-action. If a segment is ambiguous, leave it out.
2. Return each ad break as a separate segment. Do NOT merge adjacent ad breaks —
   the code will handle merging. Keeping them separate lets each be verified independently.
3. Host-read ads blend naturally into the show's tone — look for the signals
   above even when the voice and style match the rest of the episode.
4. A single ad segment should rarely exceed 3 minutes (180 seconds). If you find
   yourself marking a very long stretch as an ad, double-check it is not content.

## Reasoning step (do NOT include in output)
Before writing the JSON, briefly note each candidate segment and why you
think it is an ad. Then output ONLY the final JSON array.

## Output format
Return ONLY a valid JSON array. Each element must have "start" and "end"
keys as floating-point seconds. If there are no ads return [].

Example:
[{{"start": 118.5, "end": 197.0}}, {{"start": 2308.0, "end": 2407.5}}]

Transcript:
{transcript}
"""

_AD_HINTS_SECTION = """
## Podcast-specific ad patterns
The following notes describe known advertising patterns for this podcast.
Use them as extra guidance — do not limit detection to only these patterns.
{hints}

"""


_AD_VERIFICATION_PROMPT = """You are reviewing a candidate ad segment extracted from a podcast transcript.
The segment below was flagged as a possible advertisement.

Segment [{start:.1f}s – {end:.1f}s]:
{text}

Is this segment an advertisement or sponsored content?
Answer with ONLY a JSON object on a single line:
{{"is_ad": true, "reason": "one-sentence explanation"}}

Criteria for YES (is_ad=true):
- Clear sponsor language, discount codes, promo URLs, or calls-to-action
- Explicit mention of a product/service being sold, with pricing or sign-up incentive

Criteria for NO (is_ad=false):
- Editorial discussion, interview, news analysis, or opinion content
- Casual product mention without promotion (e.g. "I used Notion for this")
- Content about the podcast itself (subscribe, leave a review)
"""


def _verify_ad_segment(
    segment: AdSegment,
    transcript_segments: list[dict],
    bedrock_client,
    model_id: str,
) -> bool:
    """Second-pass Bedrock call to confirm a candidate ad segment.

    Used for segments above ``AD_VERIFY_THRESHOLD_SECS`` where a false positive
    would remove a significant chunk of content.

    Args:
        segment:             Candidate ad segment with ``start`` and ``end``.
        transcript_segments: Full transcript (to extract the relevant text).
        bedrock_client:      Initialised ``bedrock-runtime`` boto3 client.
        model_id:            Bedrock model ID to use for verification.

    Returns:
        ``True`` if confirmed as an ad, ``False`` if the model rejects it.
        Defaults to ``True`` on any error so we never silently discard a real ad.
    """
    text = "\n".join(
        f"[{s['start']:.1f} - {s['end']:.1f}]  {s['text']}"
        for s in transcript_segments
        if s["start"] >= segment["start"] - 5 and s["end"] <= segment["end"] + 5
    )
    if not text.strip():
        # No transcript coverage — keep the detection rather than risk missing an ad
        logger.warning(
            "[AdRemover] No transcript text for verification of [%.1f–%.1f] — keeping segment",
            segment["start"],
            segment["end"],
        )
        return True

    prompt = _AD_VERIFICATION_PROMPT.format(
        start=segment["start"],
        end=segment["end"],
        text=text[:4000],
    )

    try:
        response = retry_aws_call(
            lambda p=prompt: bedrock_client.converse(
                modelId=model_id,
                system=[
                    {
                        "text": "You are an expert podcast editor reviewing transcript segments. Output ONLY a single valid JSON object. No prose, no markdown, no explanation outside the JSON."
                    }
                ],
                messages=[
                    {"role": "user", "content": [{"text": p}]},
                    {"role": "assistant", "content": [{"text": "{"}]},
                ],
                inferenceConfig={"temperature": 0.0},
            ),
            label="bedrock.converse[verify]",
        )
        raw = "{" + response["output"]["message"]["content"][0]["text"].strip()

        # Parse the JSON response (model may wrap it in markdown)
        start_idx = raw.find("{")
        end_idx = raw.rfind("}") + 1
        if start_idx == -1 or end_idx == 0:
            logger.warning("[AdRemover] Verification response had no JSON — keeping segment: %s", raw[:200])
            return True

        result = json.loads(raw[start_idx:end_idx])
        is_ad = bool(result.get("is_ad", True))
        reason = result.get("reason", "")
        if is_ad:
            logger.info(
                "[AdRemover] Verification CONFIRMED ad [%.1f–%.1f]: %s",
                segment["start"],
                segment["end"],
                reason,
            )
        else:
            logger.info(
                "[AdRemover] Verification REJECTED [%.1f–%.1f] (not an ad): %s",
                segment["start"],
                segment["end"],
                reason,
            )
        return is_ad

    except Exception as exc:
        logger.warning(
            "[AdRemover] Verification call failed for [%.1f–%.1f]: %s — keeping segment",
            segment["start"],
            segment["end"],
            exc,
        )
        return True


_AD_NARROW_PROMPT = """ You previously identified an ad segment in a podcast transcript from {start:.1f}s to  {end:.1f}s ({duration:.0f}s total). However, this segment is too long to be a single ad —  it likely contains both regular content and one or more embedded ad reads.

Below is the transcript text for ONLY that time range. Identify the EXACT ad portion(s)  within it. Return a JSON array of the narrowed ad boundaries. If there is no actual ad  in this text, return [].

TRANSCRIPT ({start:.1f}s – {end:.1f}s):
{text}

Return ONLY a JSON array like: [{{"start": 123.4, "end": 234.5}}]
Use the original timestamps from the transcript. Be precise — only include the ad read itself,  not the surrounding content.
"""


def _narrow_oversized_segment(
    segment: AdSegment,
    transcript_segments: list[dict],
    bedrock_client,
    model_id: str,
) -> list[AdSegment]:
    """Re-send an oversized segment to Bedrock to narrow its boundaries.

    When the initial detection returns a segment that exceeds MAX_AD_SEGMENT_SECS,
    this function extracts the transcript text for that range and asks Bedrock to
    identify the precise ad portion(s) within it.

    Returns:
        A list of narrowed ad segments, or empty list on failure.
    """
    text = "\n".join(
        f"[{s['start']:.1f} - {s['end']:.1f}]  {s['text']}"
        for s in transcript_segments
        if s["start"] >= segment["start"] - 2 and s["end"] <= segment["end"] + 2
    )
    if not text.strip():
        logger.warning(
            "[AdRemover] No transcript text for narrowing [%.1f–%.1f] — cannot narrow",
            segment["start"],
            segment["end"],
        )
        return []

    duration = segment["end"] - segment["start"]
    prompt = _AD_NARROW_PROMPT.format(
        start=segment["start"],
        end=segment["end"],
        duration=duration,
        text=text[:6000],
    )

    try:
        response = retry_aws_call(
            lambda p=prompt: bedrock_client.converse(
                modelId=model_id,
                system=[
                    {
                        "text": "You are an expert podcast editor. Output ONLY valid JSON. No prose, no markdown, no commentary outside the JSON structure."
                    }
                ],
                messages=[
                    {"role": "user", "content": [{"text": p}]},
                    {"role": "assistant", "content": [{"text": "["}]},
                ],
                inferenceConfig={"temperature": 0.0},
            ),
            label="bedrock.converse[narrow]",
        )
        raw = "[" + response["output"]["message"]["content"][0]["text"].strip()
        narrowed = _parse_ad_response(raw)

        if narrowed:
            logger.info(
                "[AdRemover] Narrowed oversized segment [%.1f–%.1f] (%.0fs) → %d sub-segment(s): %s",
                segment["start"],
                segment["end"],
                duration,
                len(narrowed),
                narrowed,
            )
        else:
            logger.info(
                "[AdRemover] Narrowing found no ads within [%.1f–%.1f] — discarding",
                segment["start"],
                segment["end"],
            )
        return narrowed

    except Exception as exc:
        logger.warning(
            "[AdRemover] Narrowing call failed for [%.1f–%.1f]: %s — discarding segment",
            segment["start"],
            segment["end"],
            exc,
        )
        return []


def detect_ads(segments: list[dict], ad_hints: str = "") -> list[AdSegment]:
    """Ask AWS Bedrock to identify ad segments in *segments*.

    Uses the Bedrock Converse API with the model specified by
    ``BEDROCK_MODEL_ID`` (default: ``us.anthropic.claude-sonnet-4-6``).

    For transcripts that exceed ``AD_DETECT_MAX_CHARS``, the transcript is split
    into overlapping chunks (overlap controlled by ``AD_DETECT_OVERLAP_SECS``,
    default 60s). Each chunk is sent independently and results are merged with
    deduplication.

    Environment variables:
        AD_DETECT_MAX_CHARS     – Max transcript chars per chunk (default: 60000).
        AD_DETECT_OVERLAP_SECS  – Seconds of overlap between chunks (default: 60).

    Args:
        segments: List of transcript segment dicts as returned by
            :func:`transcribe_audio`.
        ad_hints: Optional free-text description of known ad patterns for this
            podcast (e.g. "ads always start with 'Let's hear from our sponsor'").
            Injected into the detection prompt as an extra context section when
            non-empty.  Sourced from ``PodcastConfig.ad_hints``.

    Returns:
        List of ``{"start": float, "end": float}`` dicts for ad intervals.
        Returns an empty list if the model returns no ads or response cannot
        be parsed.

    Raises:
        Exception: Propagated if the Bedrock API call itself fails (caller
            catches and falls back to original file).
    """
    if not segments:
        logger.info("[AdRemover] No transcript segments — skipping ad detection.")
        return []

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    # BEDROCK_DETECT_MODEL_ID overrides BEDROCK_MODEL_ID for detection (first-pass).
    # Use a cheaper model here (e.g. Haiku) and keep Sonnet for verification.
    _default_model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    model_id = os.environ.get("BEDROCK_DETECT_MODEL_ID", _default_model)
    max_chars = env_int("AD_DETECT_MAX_CHARS", 60000)
    overlap_secs = env_float("AD_DETECT_OVERLAP_SECS", 60.0)

    chunks = _split_segments_into_chunks(segments, max_chars, overlap_secs)
    logger.info(
        "[AdRemover] Transcript split into %d chunk(s) (max_chars=%d, overlap=%.0fs)",
        len(chunks),
        max_chars,
        overlap_secs,
    )

    bedrock = boto3.client("bedrock-runtime", region_name=region)
    all_ads: list[AdSegment] = []
    episode_duration = segments[-1]["end"] if segments else 0.0

    for i, chunk in enumerate(chunks):
        transcript_lines = "\n".join(f"[{s['start']:.1f} - {s['end']:.1f}]  {s['text']}" for s in chunk)
        hints_section = _AD_HINTS_SECTION.format(hints=ad_hints.strip()) if ad_hints and ad_hints.strip() else ""
        chunk_start = chunk[0]["start"]
        chunk_end = chunk[-1]["end"]
        context_header = (
            f"## Episode context\n"
            f"Total episode duration: {episode_duration:.0f}s | "
            f"This chunk: {chunk_start:.0f}s \u2013 {chunk_end:.0f}s "
            f"(chunk {i + 1} of {len(chunks)})\n\n"
        )
        prompt = context_header + _AD_DETECTION_PROMPT.format(transcript=transcript_lines, hints_section=hints_section)

        logger.info(
            "[AdRemover] Sending chunk %d/%d to Bedrock (model=%s, segments=%d, chars=%d)",
            i + 1,
            len(chunks),
            model_id,
            len(chunk),
            len(transcript_lines),
        )

        response = retry_aws_call(
            lambda p=prompt: bedrock.converse(
                modelId=model_id,
                system=[
                    {
                        "text": "You are an expert podcast editor. Output ONLY valid JSON. No prose, no markdown, no commentary outside the JSON structure."
                    }
                ],
                messages=[
                    {"role": "user", "content": [{"text": p}]},
                    {"role": "assistant", "content": [{"text": "["}]},
                ],
                inferenceConfig={"temperature": 0.0},
            ),
            label=f"bedrock.converse[chunk-{i + 1}]",
        )

        raw = "[" + response["output"]["message"]["content"][0]["text"]
        logger.debug("[AdRemover] Bedrock raw response (chunk %d): %s", i + 1, raw)

        chunk_ads = _parse_ad_response(raw)
        all_ads.extend(chunk_ads)

    merged = _merge_overlapping_ads(all_ads)

    # Fix #2: guard rails on duration — ads almost never exceed 5 min
    _MIN_AD_SECONDS = env_float("MIN_AD_SEGMENT_SECS", 5.0)
    max_ad_secs = env_float("MAX_AD_SEGMENT_SECS", 300.0)
    verify_threshold = env_float("AD_VERIFY_THRESHOLD_SECS", 90.0)

    valid = []
    for seg in merged:
        duration = seg["end"] - seg["start"]
        if duration < _MIN_AD_SECONDS:
            logger.warning(
                "[AdRemover] Skipping suspiciously short ad segment (%.1fs < %.1fs minimum): start=%.1f end=%.1f",
                duration,
                _MIN_AD_SECONDS,
                seg["start"],
                seg["end"],
            )
            continue
        if duration > max_ad_secs:
            # Instead of discarding, ask Bedrock to narrow boundaries
            narrowed = _narrow_oversized_segment(seg, segments, bedrock, _default_model)
            if narrowed:
                for ns in narrowed:
                    ns_dur = ns["end"] - ns["start"]
                    if _MIN_AD_SECONDS <= ns_dur <= max_ad_secs:
                        valid.append(ns)
                    else:
                        logger.warning(
                            "[AdRemover] Narrowed sub-segment still out of bounds (%.1fs): [%.1f–%.1f] — skipping",
                            ns_dur,
                            ns["start"],
                            ns["end"],
                        )
            else:
                logger.warning(
                    "[AdRemover] Skipping oversized ad segment "
                    "(%.1fs > %.1fs maximum): start=%.1f end=%.1f — narrowing failed",
                    duration,
                    max_ad_secs,
                    seg["start"],
                    seg["end"],
                )
            continue
        valid.append(seg)

    # Fix #4: second-pass verification for large segments
    if valid and verify_threshold >= 0:
        confirmed = []
        for seg in valid:
            duration = seg["end"] - seg["start"]
            if duration >= verify_threshold:
                logger.info(
                    "[AdRemover] Segment [%.1f–%.1f] (%.0fs) exceeds verify threshold "
                    "(%.0fs) — running second-pass verification",
                    seg["start"],
                    seg["end"],
                    duration,
                    verify_threshold,
                )
                if _verify_ad_segment(seg, segments, bedrock, _default_model):
                    confirmed.append(seg)
                # If rejected, it is simply dropped (logged inside _verify_ad_segment)
            else:
                confirmed.append(seg)
        valid = confirmed

    if valid and segments:
        for ad in valid:
            covered = [s["text"] for s in segments if s["start"] >= ad["start"] - 5 and s["end"] <= ad["end"] + 5]
            snippet = " ".join(covered)[:300]
            logger.info(
                "[AdRemover] Ad segment [%.1f–%.1f]: %s…",
                ad["start"],
                ad["end"],
                snippet,
            )

    logger.info("[AdRemover] Detected %d ad segment(s): %s", len(valid), valid)
    return valid


def _split_segments_into_chunks(segments: list[dict], max_chars: int, overlap_secs: float) -> list[list[dict]]:
    """Split transcript segments into chunks that fit within max_chars.

    Each chunk overlaps with the next by at least *overlap_secs* worth of
    segments so ads at chunk boundaries are seen by both chunks.
    """
    all_lines = [(s, f"[{s['start']:.1f} - {s['end']:.1f}]  {s['text']}") for s in segments]

    total_chars = sum(len(line) + 1 for _, line in all_lines)
    if total_chars <= max_chars:
        return [segments]

    chunks: list[list[dict]] = []
    i = 0
    while i < len(all_lines):
        chunk_segments: list[dict] = []
        char_count = 0

        j = i
        while j < len(all_lines):
            line_len = len(all_lines[j][1]) + 1
            if char_count + line_len > max_chars and chunk_segments:
                break
            chunk_segments.append(all_lines[j][0])
            char_count += line_len
            j += 1

        chunks.append(chunk_segments)

        if j >= len(all_lines):
            break

        # Walk back from j to find the overlap start point
        overlap_start_time = all_lines[j][0]["start"] - overlap_secs
        next_i = j
        while next_i > i and all_lines[next_i - 1][0]["start"] >= overlap_start_time:
            next_i -= 1

        # Ensure forward progress
        i = max(next_i, i + 1) if next_i <= i else next_i

    return chunks


def _coerce_ad_segment(seg: object) -> AdSegment | None:
    """Return *seg* as a well-formed ad segment, or ``None`` if it is unusable.

    Bedrock output is untrusted: it can contain nulls, strings, NaN, negative
    offsets, and inverted intervals.  An inverted segment is the dangerous case
    -- ``{"start": 500, "end": 100}`` yields keep intervals ``(0, 500)`` and
    ``(100, duration)``, so splice_audio *duplicates* 400 seconds of audio into
    the published episode instead of removing anything, with no error raised.

    Args:
        seg: A candidate segment from a parsed model response.

    Returns:
        ``{"start": float, "end": float}`` with ``0 <= start < end``, or ``None``.
    """
    if not isinstance(seg, dict) or "start" not in seg or "end" not in seg:
        logger.warning("[AdRemover] Ignoring malformed ad segment: %s", seg)
        return None
    try:
        start = float(seg["start"])
        end = float(seg["end"])
    except (TypeError, ValueError):
        logger.warning("[AdRemover] Ignoring ad segment with non-numeric bounds: %s", seg)
        return None
    if not (math.isfinite(start) and math.isfinite(end)):
        logger.warning("[AdRemover] Ignoring ad segment with non-finite bounds: %s", seg)
        return None
    if start < 0:
        logger.warning("[AdRemover] Clamping negative ad segment start %.1f to 0 (%s)", start, seg)
        start = 0.0
    if end <= start:
        logger.warning("[AdRemover] Ignoring inverted or empty ad segment: %s", seg)
        return None
    return {"start": start, "end": end}


def _clamp_ad_segments(segments: list[AdSegment], total_duration: float) -> list[AdSegment]:
    """Clamp *segments* to ``[0, total_duration]``, dropping any that fall outside.

    A segment past the end of the file makes ffmpeg's atrim produce an empty
    stream, which fails the splice or silently truncates the episode.

    Args:
        segments:       Already-coerced ad segments.
        total_duration: Probed duration of the audio file in seconds.

    Returns:
        The subset of *segments* that overlaps the file, with ends clamped.
    """
    if total_duration <= 0:
        return list(segments)

    clamped: list[AdSegment] = []
    for seg in segments:
        if seg["start"] >= total_duration:
            logger.warning(
                "[AdRemover] Dropping ad segment %.1f-%.1fs — starts past the end of the file (%.1fs)",
                seg["start"],
                seg["end"],
                total_duration,
            )
            continue
        end = min(seg["end"], total_duration)
        if end <= seg["start"]:
            continue
        if end != seg["end"]:
            logger.warning(
                "[AdRemover] Clamping ad segment end %.1fs to the file duration %.1fs",
                seg["end"],
                total_duration,
            )
        clamped.append({"start": seg["start"], "end": end})
    return clamped


def _parse_ad_response(raw: str) -> list[AdSegment]:
    """Extract a JSON array of ad segments from a model response string."""
    end_idx = raw.rfind("]")
    if end_idx == -1:
        logger.warning("[AdRemover] Bedrock response contained no JSON array — assuming no ads.")
        return []

    search_end = end_idx
    while search_end >= 0:
        start_idx = raw.rfind("[", 0, search_end)
        if start_idx == -1:
            break
        candidate = raw[start_idx : end_idx + 1]
        try:
            result = json.loads(candidate)
            if isinstance(result, list):
                valid = []
                for seg in result:
                    coerced = _coerce_ad_segment(seg)
                    if coerced is not None:
                        valid.append(coerced)
                return valid
        except json.JSONDecodeError:
            pass
        search_end = start_idx

    logger.warning("[AdRemover] Could not extract valid JSON array from Bedrock response — assuming no ads.")
    return []


def _merge_overlapping_ads(ads: list[AdSegment]) -> list[AdSegment]:
    """Merge overlapping or adjacent ad segments from multiple chunks."""
    if not ads:
        return []

    merge_gap = env_float("AD_MERGE_GAP_SECS", 2.0)
    sorted_ads = sorted(ads, key=lambda s: s["start"])
    merged: list[AdSegment] = [{"start": sorted_ads[0]["start"], "end": sorted_ads[0]["end"]}]

    for seg in sorted_ads[1:]:
        if seg["start"] <= merged[-1]["end"] + merge_gap:
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
        else:
            merged.append({"start": seg["start"], "end": seg["end"]})

    return merged


# ---------------------------------------------------------------------------
# Step 2.5 - Downloaded-file validation (ffprobe pre-flight)
# ---------------------------------------------------------------------------


def _ffprobe_duration(path: str, force_format: str | None = None) -> float:
    """Run ffprobe to get duration in seconds. Raises RuntimeError on failure."""
    cmd = ["ffprobe", "-v", "error"]
    if force_format:
        cmd += ["-f", force_format]
    cmd += [
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        timeout=_ffmpeg_timeout("FFPROBE_TIMEOUT_SECS", 60.0),
    )
    if result.stderr.strip():
        logger.debug("[AdRemover] ffprobe stderr (non-fatal): %s", result.stderr.strip())
    return float(result.stdout.strip())


def _mutagen_duration(path: str) -> float:
    """Use mutagen to read MP3 duration - pure Python, crash-proof fallback."""
    try:
        from mutagen.mp3 import MP3  # noqa: PLC0415

        return MP3(path).info.length
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"mutagen could not read duration: {exc}") from exc


def probe_duration_with_fallbacks(mp3_path: str) -> float:
    """Determine the duration (seconds) of *mp3_path* using a 3-tier fallback chain.

    Tier 1: plain ``ffprobe`` (format auto-detected).
    Tier 2: ``ffprobe -f mp3`` (handles non-standard/SSAI-stitched containers).
    Tier 3: ``mutagen`` (pure Python, immune to ffprobe crashes).

    Raises:
        RuntimeError: If all three methods fail - a strong signal the file is
                      corrupt/truncated rather than just unusually encoded.
    """
    total_duration: float | None = None
    try:
        total_duration = _ffprobe_duration(mp3_path)
    except subprocess.CalledProcessError as exc:
        stdout = (exc.stdout or "").strip()
        stderr = (exc.stderr or "").strip()
        logger.warning(
            "[AdRemover] ffprobe auto-detect failed (exit %s) for %s - retrying with -f mp3.\n  stdout: %r  stderr: %r",
            exc.returncode,
            mp3_path,
            stdout,
            stderr,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AdRemover] ffprobe error (%s): %s - retrying with -f mp3.", type(exc).__name__, exc)

    if total_duration is None:
        try:
            total_duration = _ffprobe_duration(mp3_path, force_format="mp3")
        except subprocess.CalledProcessError as exc:
            stdout = (exc.stdout or "").strip()
            stderr = (exc.stderr or "").strip()
            logger.warning(
                "[AdRemover] ffprobe -f mp3 also failed (exit %s) for %s - falling back to mutagen.\n"
                "  stdout: %r  stderr: %r",
                exc.returncode,
                mp3_path,
                stdout,
                stderr,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AdRemover] ffprobe -f mp3 error (%s): %s - falling back to mutagen.",
                type(exc).__name__,
                exc,
            )

    if total_duration is None:
        total_duration = _mutagen_duration(mp3_path)
        logger.info("[AdRemover] Used mutagen fallback for duration of %s: %.2fs", mp3_path, total_duration)

    return total_duration


def validate_audio_file(path: str, min_bytes: int = 1024) -> tuple[bool, str]:
    """Cheap pre-flight validation of a freshly downloaded audio file.

    Intended to run immediately after download, *before* the expensive
    Transcribe + Bedrock steps, so a corrupt/truncated SSAI-stitched fetch is
    caught and re-downloaded cheaply instead of discovered only when splicing
    crashes after paying for transcription and ad detection.

    Args:
        path:      Path to the downloaded audio file.
        min_bytes: Minimum acceptable file size in bytes (default 1024).

    Returns:
        ``(True, "")`` if the file looks structurally sound (exists, non-trivial
        size, and a duration can be determined via ffprobe/mutagen).
        ``(False, reason)`` otherwise, where *reason* is a short human-readable
        explanation suitable for logging.
    """
    try:
        file_size = os.path.getsize(path)
    except OSError as exc:
        return False, f"cannot stat file: {exc}"

    if file_size < min_bytes:
        return False, f"suspiciously small ({file_size} bytes < {min_bytes})"

    try:
        duration = probe_duration_with_fallbacks(path)
    except Exception as exc:  # noqa: BLE001
        return False, f"duration probe failed: {exc}"

    if duration <= 0:
        return False, f"zero/negative duration ({duration})"

    return True, ""


# ---------------------------------------------------------------------------
# Step 3 - Audio splicing (ffmpeg)
# ---------------------------------------------------------------------------


def splice_audio(mp3_path: str, ad_segments: list[AdSegment], output_path: str) -> None:
    """Remove *ad_segments* from *mp3_path* and write result to *output_path*.

    Uses ffmpeg's atrim + concat filter graph to cut the non-ad portions and
    join them into a single file.

    Args:
        mp3_path:    Path to the source audio file.
        ad_segments: List of ``{"start": float, "end": float}`` dicts to cut.
        output_path: Destination path for the cleaned audio file.

    Raises:
        RuntimeError: If ffmpeg/ffprobe is not available or returns non-zero.
        ValueError:   If *ad_segments* is empty.
    """
    if not ad_segments:
        raise ValueError("splice_audio called with empty ad_segments list")

    # Pre-flight: verify the file exists and is non-trivially sized
    try:
        file_size = os.path.getsize(mp3_path)
    except OSError as exc:
        raise RuntimeError(f"ffprobe aborted: cannot stat input file '{mp3_path}': {exc}") from exc

    logger.debug("[AdRemover] ffprobe input '%s' - size %d bytes", mp3_path, file_size)
    if file_size < 1024:
        raise RuntimeError(
            f"ffprobe aborted: input file is suspiciously small ({file_size} bytes), "
            f"likely corrupt or incomplete: '{mp3_path}'"
        )

    # Probe total duration via the shared 3-tier fallback chain (ffprobe ->
    # ffprobe -f mp3 -> mutagen); see probe_duration_with_fallbacks().
    try:
        total_duration = probe_duration_with_fallbacks(mp3_path)
    except RuntimeError as exc:
        raise RuntimeError(f"All duration-detection methods failed for '{mp3_path}': {exc}") from exc

    # Re-validate here as well as at parse time: segments also arrive from the
    # S3 ad-segment cache and from music-bookend detection, neither of which
    # goes through _parse_ad_response.
    validated = [c for c in (_coerce_ad_segment(seg) for seg in ad_segments) if c is not None]
    validated = _clamp_ad_segments(validated, total_duration)
    if not validated:
        raise ValueError(f"No usable ad segments after validation (from {len(ad_segments)} candidate(s))")

    # Sort ad segments and merge overlaps
    sorted_ads = sorted(validated, key=lambda s: s["start"])
    merged_ads: list[AdSegment] = []
    for seg in sorted_ads:
        if merged_ads and seg["start"] <= merged_ads[-1]["end"]:
            merged_ads[-1]["end"] = max(merged_ads[-1]["end"], seg["end"])
        else:
            merged_ads.append({"start": seg["start"], "end": seg["end"]})

    # Build keep intervals (inverse of ad segments)
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for ad in merged_ads:
        if ad["start"] > cursor:
            keep.append((cursor, ad["start"]))
        cursor = ad["end"]
    if cursor < total_duration:
        keep.append((cursor, total_duration))

    if not keep:
        raise RuntimeError("Ad segments cover the entire file — nothing left to splice.")

    logger.info("[AdRemover] Keep intervals (%d): %s", len(keep), keep)

    # Build ffmpeg atrim + concat + optional loudnorm filter_complex.
    # loudnorm (EBU R128) equalises loudness across all keep intervals so that
    # volume discontinuities at cut points are inaudible.
    loudnorm = os.environ.get("SPLICE_LOUDNORM", "true").lower() not in ("false", "0", "no")
    filter_parts = [
        f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]" for i, (start, end) in enumerate(keep)
    ]
    inputs = "".join(f"[a{i}]" for i in range(len(keep)))
    concat_out = "concat_out"
    filter_complex = ";".join(filter_parts) + f";{inputs}concat=n={len(keep)}:v=0:a=1[{concat_out}]"
    if loudnorm:
        filter_complex += f";[{concat_out}]loudnorm=I=-16:TP=-1.5:LRA=11[out]"
    else:
        filter_complex = filter_complex.replace(f"[{concat_out}]", "[out]", 1)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        mp3_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        output_path,
    ]

    logger.info("[AdRemover] Running ffmpeg splice command")
    logger.debug("[AdRemover] ffmpeg cmd: %s", " ".join(cmd))

    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=_ffmpeg_timeout("FFMPEG_SPLICE_TIMEOUT_SECS", 3600.0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg splice timed out after {exc.timeout}s for {mp3_path}") from exc
    except subprocess.CalledProcessError as exc:
        if exc.returncode == -11:
            # SIGSEGV — the atrim filter_complex path crashed ffmpeg (seen on
            # ARM with long files).  Retry using the concat demuxer: extract
            # each keep interval as an independent segment file, then join them.
            logger.warning(
                "[AdRemover] ffmpeg SIGSEGV (exit -11) — retrying with concat-demuxer fallback for %s",
                mp3_path,
            )
            _splice_concat_demuxer(mp3_path, keep, output_path)
        else:
            raise RuntimeError(f"ffmpeg splice failed (exit {exc.returncode}):\n{exc.stderr}") from exc


def _splice_concat_demuxer(
    mp3_path: str,
    keep: list[tuple[float, float]],
    output_path: str,
) -> None:
    """Concat-demuxer fallback for ``splice_audio``.

    Extracts each keep interval as a separate segment file using stream-copy
    (no re-encode), then joins all segments with the ``-f concat`` demuxer and
    re-encodes to MP3 with libmp3lame.  Slower than the filter_complex path but
    avoids the ARM ffmpeg SIGSEGV triggered by long filter graphs.

    Args:
        mp3_path:    Path to the source audio file.
        keep:        List of ``(start, end)`` float tuples to retain.
        output_path: Destination path for the cleaned audio file.

    Raises:
        RuntimeError: If any ffmpeg sub-command fails.
    """
    import tempfile
    import uuid

    work_dir = os.path.dirname(output_path) or tempfile.gettempdir()
    segment_paths: list[str] = []
    # os.getpid() alone is not unique: episodes are spliced concurrently by
    # threads (PODCAST_EPISODE_WORKERS) that share one PID and one work_dir, so
    # two episodes wrote to the same _seg_0_<pid>.mp3 and each other's concat
    # list, silently producing audio stitched from both episodes.
    token = f"{os.getpid()}_{uuid.uuid4().hex[:12]}"
    list_path = os.path.join(work_dir, f"_concat_{token}.txt")

    try:
        # Step 1: extract each keep interval via stream-copy
        for i, (start, end) in enumerate(keep):
            seg_path = os.path.join(work_dir, f"_seg_{i}_{token}.mp3")
            segment_paths.append(seg_path)
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                str(start),
                "-to",
                str(end),
                "-i",
                mp3_path,
                "-c:a",
                "copy",
                seg_path,
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=_ffmpeg_timeout("FFMPEG_SEGMENT_TIMEOUT_SECS", 600.0),
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"ffmpeg segment {i} extraction timed out after {exc.timeout}s") from exc
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg segment {i} extraction failed (exit {result.returncode}):\n{result.stderr}")

        # Step 2: write concat list file
        with open(list_path, "w") as fh:
            for seg_path in segment_paths:
                fh.write(f"file '{seg_path}'\n")

        # Step 3: join and re-encode
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_path,
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            output_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_ffmpeg_timeout("FFMPEG_SPLICE_TIMEOUT_SECS", 3600.0),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ffmpeg concat-demuxer join timed out after {exc.timeout}s") from exc
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat-demuxer join failed (exit {result.returncode}):\n{result.stderr}")

        logger.info("[AdRemover] concat-demuxer fallback succeeded for %s", mp3_path)

    finally:
        # Clean up temp segment files
        for seg_path in segment_paths:
            try:
                os.remove(seg_path)
            except OSError:
                pass
        try:
            os.remove(list_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _generate_summary(
    segments: list[dict],
    video_id: str,
    episode_title: str = "",
    duration_secs: float | None = None,
    cache_namespace: str = "",
) -> str:
    """Generate an AI episode summary if enabled, with S3 caching.

    Skipped when:
    - ``GENERATE_SUMMARIES`` is not ``"true"`` (default).
    - No transcript segments are provided.
    - The episode exceeds ``SUMMARY_MAX_DURATION_SECS`` (default: 1800 s = 30 min).
      Raise the limit via the env var; set to ``"0"`` to disable the guard entirely.

    Args:
        segments:      Transcript segment list (each has ``"start"``, ``"end"``,
                       ``"text"``).
        video_id:      Episode identifier — used as the S3 cache key and as a
                       fallback title when *episode_title* is empty.
        episode_title: Human-readable episode title forwarded to the Bedrock
                       prompt.  Falls back to *video_id* when empty.
        duration_secs: Total episode duration in seconds.  When provided and
                       above ``SUMMARY_MAX_DURATION_SECS``, the summary is
                       skipped.  Pass ``None`` to bypass the guard.

    Returns:
        AI-generated summary string, or ``""`` when skipped or on any failure.
    """
    if os.environ.get("GENERATE_SUMMARIES", "false").lower() not in ("true", "1", "yes"):
        return ""
    if not segments:
        return ""

    # Duration guard — skip episodes longer than the configured threshold.
    # SUMMARY_MAX_DURATION_SECS=0 disables the guard (summarise any length).
    _raw_max = os.environ.get("SUMMARY_MAX_DURATION_SECS", "1800")
    try:
        max_secs = float(_raw_max)
    except ValueError:
        logger.warning(
            "[AdRemover] SUMMARY_MAX_DURATION_SECS=%r is not a valid number — using default 1800 s",
            _raw_max,
        )
        max_secs = 1800.0
    if max_secs > 0 and duration_secs is not None and duration_secs > max_secs:
        logger.info(
            "[AdRemover] Skipping summary for %s — duration %.0fs exceeds SUMMARY_MAX_DURATION_SECS=%.0fs",
            video_id,
            duration_secs,
            max_secs,
        )
        return ""

    bucket = os.environ.get("S3_BUCKET", "")
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    if not bucket:
        return ""

    s3_client = boto3.client("s3", region_name=region)
    summary = _load_summary_cache(s3_client, bucket, video_id, cache_namespace) or ""
    if summary:
        return summary

    try:
        from summary_generator import generate_episode_summary

        title = episode_title or video_id  # use human-readable title in the Bedrock prompt
        summary = generate_episode_summary(segments, title)
        if summary:
            _save_summary_cache(s3_client, bucket, video_id, summary, cache_namespace)
    except Exception as exc:
        logger.warning("[AdRemover] Summary generation failed for %s: %s", video_id, exc)

    return summary


def remove_ads(
    mp3_path: str,
    video_id: str,
    tmp_dir: str,
    ad_hints: str = "",
    trim_music_intro: bool = False,
    trim_music_outro: bool = False,
    min_music_intro_secs: float = 8.0,
    min_music_outro_secs: float = 5.0,
    episode_title: str = "",
    duration_secs: float | None = None,
    cache_namespace: str = "",
) -> tuple[str, list[AdSegment], str]:
    """Run the full ad-removal pipeline on *mp3_path*.

    Steps:
        1. Transcribe with AWS Transcribe.
        2. Detect ads with AWS Bedrock.
        3. Splice out detected ad segments with ffmpeg.
        4. Optionally generate an AI episode summary (GENERATE_SUMMARIES=true).

    On *any* failure the function logs the error and returns the original
    *mp3_path* unchanged so the caller can still upload the unmodified file.

    Args:
        mp3_path:      Path to the downloaded audio file.
        video_id:      Episode identifier (used to name the Transcribe job and
                       output file).
        tmp_dir:       Temporary directory to write the cleaned file into.
        ad_hints:      Optional free-text hints about known ad patterns for this
                       podcast, forwarded to :func:`detect_ads`.
        episode_title: Human-readable episode title forwarded to the summary
                       Bedrock prompt.  Falls back to *video_id* when empty.
        duration_secs: Total episode duration in seconds.  Passed to the summary
                       guard — episodes longer than ``SUMMARY_MAX_DURATION_SECS``
                       are not summarised.  ``None`` bypasses the guard.
        cache_namespace: Namespace for the transcript / ad-segment / summary S3
                       caches, normally the podcast's S3 slug.  Required whenever
                       *video_id* is not globally unique (RSS guids are only
                       unique within their own feed), otherwise two shows share
                       cache entries and get each other's ad timestamps.

    Returns:
        A tuple of ``(cleaned_path, ad_segments, summary)`` where *cleaned_path*
        is the path to the cleaned audio file (or the original *mp3_path* if ad
        removal was skipped or failed), *ad_segments* is the list of detected ad
        intervals (empty list when none were found or removal was skipped), and
        *summary* is an AI-generated episode summary (empty string if disabled,
        skipped by the duration guard, or failed).
    """
    if os.environ.get("REMOVE_ADS", "true").lower() in ("false", "0", "no"):
        logger.info("[AdRemover] REMOVE_ADS=false — skipping ad removal for %s", video_id)
        return mp3_path, [], ""

    dry_run = os.environ.get("REMOVE_ADS_DRY_RUN", "false").lower() in ("true", "1", "yes")
    if dry_run:
        logger.info("[AdRemover] REMOVE_ADS_DRY_RUN=true — will detect ads but skip splicing for %s", video_id)

    # Resolve music trimming flags (kwargs override env vars)
    _trim_intro = trim_music_intro or os.environ.get("TRIM_MUSIC_INTRO", "false").lower() in ("true", "1", "yes")
    _trim_outro = trim_music_outro or os.environ.get("TRIM_MUSIC_OUTRO", "false").lower() in ("true", "1", "yes")
    _min_intro = (
        min_music_intro_secs if min_music_intro_secs != 8.0 else env_float("MUSIC_INTRO_MIN_SECS", 8.0)
    )
    _min_outro = (
        min_music_outro_secs if min_music_outro_secs != 5.0 else env_float("MUSIC_OUTRO_MIN_SECS", 5.0)
    )

    logger.info("[AdRemover] Starting ad removal for %s", video_id)

    # Check ad-segments cache first (requires transcript cache to be enabled too).
    # If both transcript and ad-segments are cached, skip Transcribe + Bedrock entirely.
    cache_enabled = os.environ.get("TRANSCRIBE_CACHE_ENABLED", "true").lower() not in ("false", "0", "no")
    use_cache = cache_enabled and not video_id.startswith("eval-")
    ad_segments: list[AdSegment] = []
    segments: list[dict] = []

    # Create S3 client once for all cache operations in this function
    _bucket = os.environ.get("S3_BUCKET", "")
    _region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    _s3 = boto3.client("s3", region_name=_region) if _bucket else None

    if use_cache and _bucket and _s3:
        cached_ads = _load_ad_segments_cache(_s3, _bucket, video_id, cache_namespace)
        if cached_ads is not None:
            ad_segments = cached_ads
            # Skip transcription + detection — jump straight to snap + splice
            logger.info("[AdRemover] Using cached ad-segments for %s — skipping Transcribe+Bedrock", video_id)

            # Load cached transcript for music-bookend detection and/or summary
            # generation — a single S3 read serves both consumers.
            music_segments: list[AdSegment] = []
            _cached_transcript: list[dict] = []
            _want_cached_transcript = (_trim_intro or _trim_outro) or os.environ.get(
                "GENERATE_SUMMARIES", "false"
            ).lower() in ("true", "1", "yes")
            if _want_cached_transcript:
                _loaded_transcript = _load_transcript_cache(_s3, _bucket, video_id, cache_namespace)
                if _loaded_transcript:
                    _cached_transcript = _loaded_transcript
            if (_trim_intro or _trim_outro) and _cached_transcript:
                try:
                    music_segments = detect_music_bookends(
                        _cached_transcript,
                        mp3_path,
                        min_intro_secs=_min_intro if _trim_intro else 9999.0,
                        min_outro_secs=_min_outro if _trim_outro else 9999.0,
                    )
                except Exception as exc:
                    logger.warning("[AdRemover] Music detection failed (cached path) for %s: %s", video_id, exc)

            all_cached = _merge_overlapping_ads(ad_segments + music_segments)
            if not all_cached:
                logger.info("[AdRemover] Cached ad-segments empty (no ads) for %s — using original file", video_id)
                return (
                    mp3_path,
                    [],
                    _generate_summary(_cached_transcript, video_id, episode_title, duration_secs, cache_namespace=cache_namespace),
                )
            if os.environ.get("AD_SNAP_TO_SILENCE", "true").lower() not in ("false", "0", "no"):
                all_cached = snap_ad_boundaries(all_cached, mp3_path)
            if dry_run:
                total_ad_secs = sum(s["end"] - s["start"] for s in all_cached)
                logger.info(
                    "[AdRemover] DRY-RUN: would remove %d cached segment(s) totalling %.1fs from %s",
                    len(all_cached),
                    total_ad_secs,
                    video_id,
                )
                return mp3_path, all_cached, ""
            cleaned_path = os.path.join(tmp_dir, f"{video_id}_clean.mp3")
            try:
                splice_audio(mp3_path, all_cached, cleaned_path)
            except Exception as exc:
                logger.error("[AdRemover] Splicing failed for %s: %s — using original file", video_id, exc)
                return (
                    mp3_path,
                    all_cached,
                    _generate_summary(_cached_transcript, video_id, episode_title, duration_secs, cache_namespace=cache_namespace),
                )
            logger.info("[AdRemover] Ad removal complete (cached) for %s → %s", video_id, cleaned_path)
            return (
                cleaned_path,
                all_cached,
                _generate_summary(_cached_transcript, video_id, episode_title, duration_secs, cache_namespace=cache_namespace),
            )

    try:
        segments = transcribe_audio(mp3_path, video_id, cache_namespace=cache_namespace)
    except Exception as exc:
        logger.error("[AdRemover] Transcription failed for %s: %s — using original file", video_id, exc, exc_info=True)
        return mp3_path, [], "TRANSCRIBE_FAILED"

    try:
        ad_segments = detect_ads(segments, ad_hints=ad_hints)
    except Exception as exc:
        logger.error("[AdRemover] Ad detection failed for %s: %s — using original file", video_id, exc, exc_info=True)
        return mp3_path, [], "DETECT_FAILED"

    # Save detection result to cache so retries (after splice failure) skip Bedrock
    if use_cache and _bucket and _s3:
        _save_ad_segments_cache(_s3, _bucket, video_id, ad_segments, cache_namespace)

    # Detect silence once for reuse in both music detection and boundary snapping
    _silences: list[dict] | None = None
    _need_silence = (_trim_intro or _trim_outro) or os.environ.get("AD_SNAP_TO_SILENCE", "true").lower() not in (
        "false",
        "0",
        "no",
    )
    if _need_silence:
        try:
            _silences = detect_silence(mp3_path)
        except Exception as exc:
            logger.warning("[AdRemover] Silence detection failed for %s: %s", video_id, exc)
            _silences = []

    # Detect music bookends (intro/outro) if enabled — merged with ad_segments for a single splice
    music_segments: list[AdSegment] = []
    if (_trim_intro or _trim_outro) and segments:
        try:
            music_segments = detect_music_bookends(
                segments,
                mp3_path,
                min_intro_secs=_min_intro if _trim_intro else 9999.0,
                min_outro_secs=_min_outro if _trim_outro else 9999.0,
                silences=_silences,
            )
        except Exception as exc:
            logger.warning(
                "[AdRemover] Music bookend detection failed for %s: %s — proceeding with ads only", video_id, exc
            )

    # Merge ad + music segments for a single splice pass
    all_segments = _merge_overlapping_ads(ad_segments + music_segments)

    if not all_segments:
        logger.info("[AdRemover] No ads detected for %s — using original file", video_id)
        summary = _generate_summary(segments, video_id, episode_title, duration_secs, cache_namespace=cache_namespace)
        return mp3_path, [], summary

    # Fix #5: snap boundaries to silence gaps for cleaner cuts
    if os.environ.get("AD_SNAP_TO_SILENCE", "true").lower() not in ("false", "0", "no"):
        all_segments = snap_ad_boundaries(all_segments, mp3_path, silences=_silences)

    if dry_run:
        total_secs = sum(s["end"] - s["start"] for s in all_segments)
        logger.info(
            "[AdRemover] DRY-RUN: would remove %d segment(s) totalling %.1fs from %s — skipping splice",
            len(all_segments),
            total_secs,
            video_id,
        )
        return mp3_path, all_segments, ""

    cleaned_path = os.path.join(tmp_dir, f"{video_id}_clean.mp3")
    try:
        splice_audio(mp3_path, all_segments, cleaned_path)
    except Exception as exc:
        logger.error("[AdRemover] Splicing failed for %s: %s — using original file", video_id, exc, exc_info=True)
        return mp3_path, all_segments, "SPLICE_FAILED"

    summary = _generate_summary(segments, video_id, episode_title, duration_secs, cache_namespace=cache_namespace)

    logger.info("[AdRemover] Ad removal complete for %s → %s", video_id, cleaned_path)
    return cleaned_path, all_segments, summary
