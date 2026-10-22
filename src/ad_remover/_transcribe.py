"""Step 1 of the pipeline: AWS Transcribe orchestration.

Covers audio-duration probing, ``AD_TRANSCRIBE_WINDOWS`` parsing and sub-clip
extraction, the shared Transcribe job runner, the public ``transcribe_audio``
entry point, and the word-item → phrase-segment collapse.

Patched-symbol routing: ``boto3``, ``retry_aws_call``, ``_load_transcript_cache``,
``_get_audio_duration`` and ``_extract_audio_window`` are rebound on the
``ad_remover`` package by the test suite, so this module calls them through the
package namespace (``ad_remover.<name>``). Helpers that are *not* patched
(``_ffmpeg_bin``, ``_ffprobe_bin``, ``_safe_namespace``, the cache *writers*,
``_items_to_segments``) are imported/called directly. ``subprocess`` is imported
normally — tests patch ``ad_remover.subprocess.run`` on the shared module.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import uuid

import certifi

import settings
from ad_remover._cache import _save_transcript_cache, _save_transcript_text
from ad_remover._cachekeys import _safe_namespace
from ad_remover._ffmpeg import _ffmpeg_bin, _ffprobe_bin

logger = logging.getLogger("ad_remover")


def _pkg():
    """Return the live ``ad_remover`` package module (see _ffmpeg._pkg)."""
    return sys.modules["ad_remover"]


_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

_MAX_POLL_ERRORS = 5


def _get_audio_duration(mp3_path: str) -> float:
    """Return the duration of *mp3_path* in seconds using ffprobe.

    Returns ``0.0`` if ffprobe fails so callers can fall back gracefully.
    """
    try:
        result = subprocess.run(
            [
                _ffprobe_bin(),
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
        _ffmpeg_bin(),
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


def _run_transcribe_job(
    s3_client,
    transcribe_client,
    *,
    bucket: str,
    local_path: str,
    clip_id: str,
    namespace: str,
    language_code: str,
    poll_interval: int,
    max_wait: int,
    label_suffix: str = "",
) -> list[dict]:
    """Transcribe one audio file with AWS Transcribe and return its segments.

    Shared by the full-file and the windowed (``AD_TRANSCRIBE_WINDOWS``) paths.
    The windowed path used to be a second hand-written copy of this flow that had
    drifted: it namespaced no S3 keys (so two feeds reusing an episode id raced on
    ``transcribe-tmp/``) and tolerated no polling errors (so a single throttled
    ``get_transcription_job`` aborted the whole episode).  Both paths now inherit
    the fixes from one place.

    The temporary S3 object and the Transcribe job are always deleted, including on
    failure.  ``local_path`` is never touched — the caller owns it.

    Returns:
        Segment dicts with ``start``/``end``/``text``, timestamps relative to
        ``local_path``.

    Raises:
        RuntimeError: If the job fails, times out, or polling fails repeatedly.
    """
    from botocore.exceptions import ClientError

    # Namespaced so two feeds that reuse an episode id cannot overwrite each
    # other's in-flight Transcribe input while both jobs are running.
    ns = _safe_namespace(namespace)
    tmp_key = f"transcribe-tmp/{ns}/{clip_id}.mp3" if ns else f"transcribe-tmp/{clip_id}.mp3"
    logger.info("[AdRemover] Uploading %s to s3://%s/%s for transcription", local_path, bucket, tmp_key)
    _pkg().retry_aws_call(
        lambda: s3_client.upload_file(local_path, bucket, tmp_key),
        label=f"s3.upload_file[transcribe-tmp{label_suffix}]",
    )

    media_uri = f"s3://{bucket}/{tmp_key}"
    # Transcribe job names only allow [A-Za-z0-9_-] — sanitize the clip id
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", clip_id)[:64].strip("-")
    job_name = f"pad-{safe_id}-{uuid.uuid4().hex[:8]}"

    try:
        logger.info("[AdRemover] Starting Transcribe job %s", job_name)
        _pkg().retry_aws_call(
            lambda: transcribe_client.start_transcription_job(
                TranscriptionJobName=job_name,
                Media={"MediaFileUri": media_uri},
                MediaFormat="mp3",
                LanguageCode=language_code,
                Settings={"ShowSpeakerLabels": False, "ChannelIdentification": False},
            ),
            label=f"transcribe.start_transcription_job{label_suffix}",
        )

        elapsed = 0
        consecutive_poll_errors = 0
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

        import urllib.parse
        import urllib.request

        transcript_uri = status_resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
        # AWS returns a presigned URL here. Its query string contains temporary
        # credentials, so retain only the origin and path in retained logs.
        parsed_transcript_uri = urllib.parse.urlsplit(transcript_uri)
        safe_transcript_uri = urllib.parse.urlunsplit(
            (parsed_transcript_uri.scheme, parsed_transcript_uri.netloc, parsed_transcript_uri.path, "", "")
        )
        logger.info("[AdRemover] Downloading transcript from %s", safe_transcript_uri)

        with urllib.request.urlopen(transcript_uri, context=_SSL_CTX) as resp:
            transcript_data = json.loads(resp.read())

        items = transcript_data.get("results", {}).get("items", [])
        segments = _items_to_segments(items)
        logger.info("[AdRemover] Transcription complete: %d segments from %d items", len(segments), len(items))
        return segments

    finally:
        try:
            s3_client.delete_object(Bucket=bucket, Key=tmp_key)
            logger.debug("[AdRemover] Deleted temp S3 object s3://%s/%s", bucket, tmp_key)
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
            logger.warning("[AdRemover] Could not delete temp S3 object: %s", exc)

        try:
            transcribe_client.delete_transcription_job(TranscriptionJobName=job_name)
            logger.debug("[AdRemover] Deleted Transcribe job %s", job_name)
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
            logger.warning("[AdRemover] Could not delete Transcribe job %s: %s", job_name, exc)


def transcribe_audio(mp3_path: str, video_id: str, cache_namespace: str = "") -> list[dict]:
    """Upload *mp3_path* to S3 and transcribe it with AWS Transcribe.

    Returns a list of segment dicts, each with keys ``start`` (float),
    ``end`` (float), and ``text`` (str) derived from the word-level items
    in the Transcribe output.

    When ``AD_TRANSCRIBE_WINDOWS`` is set, only the named sub-clips are
    transcribed (to reduce Transcribe cost) and their timestamps are offset back
    onto the full-episode timeline.

    Args:
        mp3_path: Local path to the audio file.
        video_id: Episode identifier, used to name the temporary S3 object and
                  the Transcribe job.
        cache_namespace: Feed-scoped prefix so two feeds reusing an episode id
                  do not share cache or temp-upload keys.

    Returns:
        List of segment dicts.  Empty list when Transcribe produces no output.

    Raises:
        RuntimeError: On any AWS API error or if the Transcribe job fails.
    """
    bucket = settings.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET must be set to use AWS Transcribe")

    region = settings.get("AWS_DEFAULT_REGION")
    language_code = settings.get("TRANSCRIBE_LANGUAGE_CODE")
    poll_interval = settings.get("TRANSCRIBE_POLL_INTERVAL")
    max_wait = settings.get("TRANSCRIBE_MAX_WAIT")
    cache_enabled = settings.get("TRANSCRIBE_CACHE_ENABLED")
    # Skip caching for evaluator re-transcriptions (eval- prefix) — those target the cleaned
    # file and should not overwrite the original episode's cache entry.
    use_cache = cache_enabled and not video_id.startswith("eval-")

    s3_client = _pkg().boto3.client("s3", region_name=region)
    transcribe_client = _pkg().boto3.client("transcribe", region_name=region)

    # 0. Check transcript cache (skip expensive Transcribe job if already done)
    if use_cache:
        cached = _pkg()._load_transcript_cache(s3_client, bucket, video_id, cache_namespace)
        if cached is not None:
            return cached

    def _run(local_path: str, clip_id: str, label_suffix: str = "") -> list[dict]:
        return _run_transcribe_job(
            s3_client,
            transcribe_client,
            bucket=bucket,
            local_path=local_path,
            clip_id=clip_id,
            namespace=cache_namespace,
            language_code=language_code,
            poll_interval=poll_interval,
            max_wait=max_wait,
            label_suffix=label_suffix,
        )

    def _cache(segments: list[dict]) -> None:
        if use_cache:
            _save_transcript_cache(s3_client, bucket, video_id, segments, cache_namespace)
            _save_transcript_text(s3_client, bucket, video_id, segments, cache_namespace)

    # 1a. Selective transcription windows (AD_TRANSCRIBE_WINDOWS)
    #     If set, only transcribe specified sub-clips to reduce Transcribe cost.
    windows_raw = settings.get("AD_TRANSCRIBE_WINDOWS").strip()
    if windows_raw:
        duration = _pkg()._get_audio_duration(mp3_path)
        windows = _parse_transcribe_windows(windows_raw, duration)
        if windows:
            all_segments: list[dict] = []
            for w_idx, (w_start, w_end) in enumerate(windows):
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as w_tmp:
                    w_tmp_path = w_tmp.name
                try:
                    _pkg()._extract_audio_window(mp3_path, w_start, w_end, w_tmp_path)
                    logger.info(
                        "[AdRemover] Window %d/%d: uploading %.1f-%.1fs sub-clip for transcription",
                        w_idx + 1,
                        len(windows),
                        w_start,
                        w_end,
                    )
                    w_segs = _run(w_tmp_path, f"{video_id}-w{w_idx}", label_suffix=f"[window-{w_idx}]")
                    # Offset window-relative timestamps back onto the episode timeline
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

            all_segments.sort(key=lambda s: s["start"])
            logger.info(
                "[AdRemover] Windowed transcription complete: %d total segments from %d window(s)",
                len(all_segments),
                len(windows),
            )
            _cache(all_segments)
            return all_segments

    # 1b. Whole-file transcription
    segments = _run(mp3_path, video_id)
    _cache(segments)
    return segments


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
