"""Ad removal pipeline for downloaded podcast audio.

Pipeline:
    1. Upload the audio file to a temporary S3 prefix and transcribe it with
       AWS Transcribe (word-level timestamps).
    2. Send the transcript to AWS Bedrock (model from BEDROCK_DETECT_MODEL_ID,
       falling back to BEDROCK_MODEL_ID, via the Converse API) to identify
       ad-segment timestamps as JSON.
    3. Use ffmpeg to splice out the ad segments and stitch the remaining audio
       into a single output MP3.
    4. Fall back to the original file if any step fails.

The implementation is split across submodules (all re-exported here so callers
and tests keep using ``ad_remover.<name>``):

    _prompts     Bedrock prompt templates.
    _segments    Pure ad-segment math and model-response parsing.
    _cachekeys   S3 cache-key construction.
    _cache       S3 read/write for the transcript/summary/ad-segment caches.
    _ffmpeg      ffmpeg process helpers, silence and music detection.
    _transcribe  AWS Transcribe orchestration (Step 1).
    _detect      Bedrock ad detection/verification/narrowing (Step 2).
    _splice      Duration probing, validation and ffmpeg splicing (Steps 2.5/3).

This package __init__ keeps the orchestrator (``remove_ads``), the summary
helper, and the shared error types. Cross-module calls to functions the test
suite rebinds on this package (``transcribe_audio``, ``detect_ads``,
``splice_audio``, ``boto3``, ``retry_aws_call``, ...) are made through the
``ad_remover`` namespace so ``patch("ad_remover.<name>")`` is honoured.

Environment variables (all optional — sensible defaults provided):
    REMOVE_ADS              – Set to "false" to skip ad removal entirely (default: "true").
    S3_BUCKET               – Bucket used for the temporary Transcribe input file
                              (re-uses the existing podcast bucket).
    AWS_DEFAULT_REGION      – AWS region for Transcribe and Bedrock clients.
    TRANSCRIBE_LANGUAGE_CODE – BCP-47 language code passed to Transcribe (default: "en-US").
    BEDROCK_MODEL_ID        – Bedrock model ID for ad-segment verification (second-pass
                              confirmation of long segments) and episode summaries
                              (default: "us.anthropic.claude-haiku-4-5-20251001-v1:0").
    BEDROCK_DETECT_MODEL_ID – Bedrock model ID for first-pass ad detection across all chunks.
                              Defaults to BEDROCK_MODEL_ID when not set.  Set a different
                              model here to tune the cost/accuracy split between the
                              detection and verification passes.
    TRANSCRIBE_POLL_INTERVAL – Seconds between Transcribe job status polls (default: 10).
    TRANSCRIBE_MAX_WAIT     – Maximum seconds to wait for a Transcribe job (default: 3600).
    REMOVE_ADS_DRY_RUN      – Set to "true" to detect ads and log them without actually
                              splicing the audio file (default: "false").  Useful for
                              evaluating detection quality before enabling full removal.
    MAX_AD_SEGMENT_SECS     – Ad segments longer than this are treated as false positives and
                              skipped (default: "300").  Legitimate podcast ads rarely exceed
                              a few minutes; very long segments usually indicate content
                              misclassified as an ad.
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

import logging
import os
import subprocess

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import settings

# Pure, dependency-light helpers (no AWS/ffmpeg). Imported first because the
# implementation submodules below import from them.
from ad_remover._cachekeys import (
    _cache_key,  # noqa: F401 - re-exported for callers/tests
    _cache_read_keys,  # noqa: F401 - re-exported for callers/tests
    _safe_namespace,  # noqa: F401 - re-exported for callers/tests
    _transcript_cache_key,  # noqa: F401 - re-exported for callers/tests
)
from ad_remover._prompts import (
    _AD_DETECTION_PROMPT,  # noqa: F401 - re-exported for callers/tests
    _AD_HINTS_SECTION,  # noqa: F401 - re-exported for callers/tests
    _AD_NARROW_PROMPT,  # noqa: F401 - re-exported for callers/tests
    _AD_VERIFICATION_PROMPT,  # noqa: F401 - re-exported for callers/tests
)
from ad_remover._segments import (
    AdSegment,
    _clamp_ad_segments,  # noqa: F401 - re-exported for callers/tests
    _coerce_ad_segment,  # noqa: F401 - re-exported for callers/tests
    _merge_overlapping_ads,
    _parse_ad_response,  # noqa: F401 - re-exported for callers/tests
    _split_segments_into_chunks,  # noqa: F401 - re-exported for callers/tests
)
from utils import retry_aws_call  # noqa: F401 - re-exported as the ad_remover.retry_aws_call patch target

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class NoUsableAdSegments(ValueError):
    """Every candidate ad segment was value-invalid, so there is nothing to cut.

    Raised only for defects in the segment values themselves -- malformed,
    non-numeric, non-finite, inverted or zero-length.  That verdict depends on
    nothing but the values, so re-downloading and re-splicing produces the same
    outcome on every attempt and every later cron run.  Treating it as a splice
    failure therefore blocks the episode forever; ``remove_ads`` instead reports
    "no cuts" so the original file is published once and the episode is done.

    Segments lost to duration clamping are **not** this error: well-formed
    timestamps that fall outside the probed audio mean the download is truncated,
    which ``splice_audio`` reports as a ``RuntimeError`` so the existing retry
    path gets a fresh download instead of publishing a truncated episode.

    Subclasses ``ValueError`` so existing callers that absorb operational
    errors keep working unchanged.
    """


class FfmpegCrashed(RuntimeError):
    """ffmpeg died on a signal (SIGSEGV/SIGABRT) on every splice strategy.

    A signal crash is a fault of the ffmpeg build on this host, not of the
    episode: the same bytes splice cleanly on a healthy binary.  Callers must
    hold the episode for a healthy run instead of publishing the original with
    its ads, and should stop splicing for the rest of the process.
    """


#: Signal exit codes that mean the ffmpeg/ffprobe binary itself crashed.
FFMPEG_CRASH_RETURNCODES: frozenset[int] = frozenset({-11, -6})

#: Error code :func:`remove_ads` returns when the splice failed with
#: :class:`FfmpegCrashed`.
FFMPEG_CRASHED = "FFMPEG_CRASHED"

_ffmpeg_crash_reason: str | None = None


def ffmpeg_crash_reason() -> str | None:
    """Return why splicing is disabled in this process, or None if ffmpeg is trusted."""
    return _ffmpeg_crash_reason


def _record_ffmpeg_crash(reason: str) -> None:
    global _ffmpeg_crash_reason
    if _ffmpeg_crash_reason is None:
        logger.error(
            "[AdRemover] ffmpeg on this host crashes on real audio (%s) — holding every remaining "
            "episode that needs ad removal for a healthy run instead of publishing originals with ads",
            reason,
        )
        _ffmpeg_crash_reason = reason


def _reset_ffmpeg_crash() -> None:
    global _ffmpeg_crash_reason
    _ffmpeg_crash_reason = None


#: Sentinel strings returned by :func:`remove_ads` in the summary position when
#: a pipeline stage fails.  Callers **must** exclude these before writing the
#: value to the episode manifest or an RSS feed — they are error signals, not
#: human-readable content.
REMOVE_ADS_ERROR_CODES: frozenset[str] = frozenset(
    {
        "TRANSCRIBE_FAILED",
        "DETECT_FAILED",
        "SPLICE_FAILED",
        FFMPEG_CRASHED,
    }
)


# Implementation submodules. Imported AFTER NoUsableAdSegments and the pure
# helpers above, because _splice imports NoUsableAdSegments from this package
# and every impl submodule does ``import ad_remover`` to route patched calls
# back through this namespace. Each name is re-exported so callers and tests
# keep referring to it as ``ad_remover.<name>`` and ``patch("ad_remover.<name>")``
# continues to patch the symbol the orchestrator and submodules actually call.
from ad_remover._cache import (  # noqa: E402
    _get_cached_bytes,  # noqa: F401 - re-exported for callers/tests
    _load_ad_segments_cache,
    _load_summary_cache,
    _load_transcript_cache,
    _save_ad_segments_cache,
    _save_summary_cache,
    _save_transcript_cache,  # noqa: F401 - re-exported for callers/tests
    _save_transcript_text,  # noqa: F401 - re-exported for callers/tests
)
from ad_remover._detect import (  # noqa: E402
    _narrow_oversized_segment,  # noqa: F401 - re-exported for callers/tests
    _verify_ad_segment,  # noqa: F401 - re-exported for callers/tests
    detect_ads,
)
from ad_remover._ffmpeg import (  # noqa: E402
    _ffmpeg_bin,  # noqa: F401 - re-exported for callers/tests
    _ffmpeg_timeout,  # noqa: F401 - re-exported for callers/tests
    _ffprobe_bin,  # noqa: F401 - re-exported for callers/tests
    _snap_to_silence_boundary,  # noqa: F401 - re-exported for callers/tests
    detect_music_bookends,
    detect_silence,
    snap_ad_boundaries,
)
from ad_remover._splice import (  # noqa: E402
    _ffprobe_duration,  # noqa: F401 - re-exported for callers/tests
    _mutagen_duration,  # noqa: F401 - re-exported for callers/tests
    _splice_concat_demuxer,  # noqa: F401 - re-exported for callers/tests
    probe_duration_with_fallbacks,  # noqa: F401 - re-exported for callers/tests
    splice_audio,
    validate_audio_file,  # noqa: F401 - re-exported for callers/tests
)
from ad_remover._transcribe import (  # noqa: E402
    _MAX_POLL_ERRORS,  # noqa: F401 - re-exported for callers/tests
    _extract_audio_window,  # noqa: F401 - re-exported for callers/tests
    _get_audio_duration,  # noqa: F401 - re-exported for callers/tests
    _items_to_segments,  # noqa: F401 - re-exported for callers/tests
    _parse_transcribe_windows,  # noqa: F401 - re-exported for callers/tests
    _run_transcribe_job,  # noqa: F401 - re-exported for callers/tests
    transcribe_audio,
)

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
    if not settings.get("GENERATE_SUMMARIES"):
        return ""
    if not segments:
        return ""

    # Duration guard — skip episodes longer than the configured threshold.
    # SUMMARY_MAX_DURATION_SECS=0 disables the guard (summarise any length).
    # A malformed value warns and falls back to the declared default inside
    # settings.get(), so no local try/except is needed.
    max_secs = float(settings.get("SUMMARY_MAX_DURATION_SECS"))
    if max_secs > 0 and duration_secs is not None and duration_secs > max_secs:
        logger.info(
            "[AdRemover] Skipping summary for %s — duration %.0fs exceeds SUMMARY_MAX_DURATION_SECS=%.0fs",
            video_id,
            duration_secs,
            max_secs,
        )
        return ""

    bucket = settings.get("S3_BUCKET")
    region = settings.get("AWS_DEFAULT_REGION")
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


# Failures we expect from the outside world (ffmpeg, AWS, network, malformed
# responses) and can absorb inside remove_ads() by falling back to the original
# file.  Anything outside this tuple — TypeError, AttributeError, KeyError and
# friends — is a bug in this module rather than an outage, and must NOT be quietly
# downgraded to "published the episode with its ads intact".  Both callers wrap each
# episode in their own handler, so a real bug surfaces as one failed episode in
# the run summary instead of a silent, ad-laden upload.
_OPERATIONAL_ERRORS = (
    OSError,          # file/IO, and the base of ConnectionError / TimeoutError
    RuntimeError,     # what this module raises for AWS/ffmpeg failures
    ValueError,       # includes json.JSONDecodeError
    subprocess.SubprocessError,
    BotoCoreError,
    ClientError,
)


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
    if not settings.get("REMOVE_ADS"):
        logger.info("[AdRemover] REMOVE_ADS=false — skipping ad removal for %s", video_id)
        return mp3_path, [], ""

    dry_run = settings.get("REMOVE_ADS_DRY_RUN")
    if dry_run:
        logger.info("[AdRemover] REMOVE_ADS_DRY_RUN=true — will detect ads but skip splicing for %s", video_id)

    # Resolve music trimming flags (kwargs override env vars)
    _trim_intro = trim_music_intro or settings.get("TRIM_MUSIC_INTRO")
    _trim_outro = trim_music_outro or settings.get("TRIM_MUSIC_OUTRO")
    _min_intro = (
        min_music_intro_secs if min_music_intro_secs != 8.0 else settings.get("MUSIC_INTRO_MIN_SECS")
    )
    _min_outro = (
        min_music_outro_secs if min_music_outro_secs != 5.0 else settings.get("MUSIC_OUTRO_MIN_SECS")
    )

    logger.info("[AdRemover] Starting ad removal for %s", video_id)

    # Check ad-segments cache first (requires transcript cache to be enabled too).
    # If both transcript and ad-segments are cached, skip Transcribe + Bedrock entirely.
    cache_enabled = settings.get("TRANSCRIBE_CACHE_ENABLED")
    use_cache = cache_enabled and not video_id.startswith("eval-")
    ad_segments: list[AdSegment] = []
    segments: list[dict] = []

    # Create S3 client once for all cache operations in this function
    _bucket = settings.get("S3_BUCKET")
    _region = settings.get("AWS_DEFAULT_REGION")
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
            _want_cached_transcript = (_trim_intro or _trim_outro) or settings.get("GENERATE_SUMMARIES")
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
                except _OPERATIONAL_ERRORS as exc:
                    logger.warning("[AdRemover] Music detection failed (cached path) for %s: %s", video_id, exc)

            all_cached = _merge_overlapping_ads(ad_segments + music_segments)
            if not all_cached:
                logger.info("[AdRemover] Cached ad-segments empty (no ads) for %s — using original file", video_id)
                return (
                    mp3_path,
                    [],
                    _generate_summary(_cached_transcript, video_id, episode_title, duration_secs, cache_namespace=cache_namespace),
                )
            if settings.get("AD_SNAP_TO_SILENCE"):
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
            except NoUsableAdSegments as exc:
                # Deterministic: the cached timestamps can never splice against
                # this audio, so retrying would block the episode forever.
                # Report no cuts and let the caller publish the original once.
                logger.warning(
                    "[AdRemover] Cached ad-segments unusable for %s (%s) — publishing original file",
                    video_id,
                    exc,
                )
                return (
                    mp3_path,
                    [],
                    _generate_summary(_cached_transcript, video_id, episode_title, duration_secs, cache_namespace=cache_namespace),
                )
            except _OPERATIONAL_ERRORS as exc:
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
    except _OPERATIONAL_ERRORS as exc:
        logger.error("[AdRemover] Transcription failed for %s: %s — using original file", video_id, exc, exc_info=True)
        return mp3_path, [], "TRANSCRIBE_FAILED"

    try:
        ad_segments = detect_ads(segments, ad_hints=ad_hints)
    except _OPERATIONAL_ERRORS as exc:
        logger.error("[AdRemover] Ad detection failed for %s: %s — using original file", video_id, exc, exc_info=True)
        return mp3_path, [], "DETECT_FAILED"

    # Save detection result to cache so retries (after splice failure) skip Bedrock
    if use_cache and _bucket and _s3:
        _save_ad_segments_cache(_s3, _bucket, video_id, ad_segments, cache_namespace)

    # Detect silence once for reuse in both music detection and boundary snapping
    _silences: list[dict] | None = None
    _need_silence = (_trim_intro or _trim_outro) or settings.get("AD_SNAP_TO_SILENCE")
    if _need_silence:
        try:
            _silences = detect_silence(mp3_path)
        except _OPERATIONAL_ERRORS as exc:
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
        except _OPERATIONAL_ERRORS as exc:
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
    if settings.get("AD_SNAP_TO_SILENCE"):
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
    except NoUsableAdSegments as exc:
        # Not a splice failure: nothing survived validation, so there is nothing
        # ffmpeg could have cut.  Returning an empty segment list keeps this
        # distinguishable from a genuine ffmpeg crash (which still returns the
        # detected segments plus SPLICE_FAILED) and lets the original publish.
        logger.warning("[AdRemover] No usable ad segments for %s (%s) — using original file", video_id, exc)
        summary = _generate_summary(segments, video_id, episode_title, duration_secs, cache_namespace=cache_namespace)
        return mp3_path, [], summary
    except FfmpegCrashed as exc:
        _record_ffmpeg_crash(f"{video_id}: {exc}")
        logger.error("[AdRemover] Splicing crashed for %s: %s — holding the episode", video_id, exc)
        return mp3_path, all_segments, FFMPEG_CRASHED
    except _OPERATIONAL_ERRORS as exc:
        logger.error("[AdRemover] Splicing failed for %s: %s — using original file", video_id, exc, exc_info=True)
        return mp3_path, all_segments, "SPLICE_FAILED"

    summary = _generate_summary(segments, video_id, episode_title, duration_secs, cache_namespace=cache_namespace)

    logger.info("[AdRemover] Ad removal complete for %s → %s", video_id, cleaned_path)
    return cleaned_path, all_segments, summary
