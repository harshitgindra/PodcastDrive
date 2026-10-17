"""Step 2 of the pipeline: Bedrock ad detection, verification and narrowing.

``detect_ads`` chunks the transcript, asks Bedrock for ad intervals, then runs
the duration guard, oversized-segment narrowing and second-pass verification.

Patched-symbol routing: ``boto3`` and ``retry_aws_call`` are rebound on the
``ad_remover`` package by the test suite, so this module reaches them through
the package namespace. ``_verify_ad_segment`` and ``_narrow_oversized_segment``
are not patched and live here, so ``detect_ads`` calls them directly. The
prompt templates and pure segment helpers are imported from their submodules.
"""

from __future__ import annotations

import json
import logging
import sys

import settings
from ad_remover._prompts import (
    _AD_DETECTION_PROMPT,
    _AD_HINTS_SECTION,
    _AD_NARROW_PROMPT,
    _AD_VERIFICATION_PROMPT,
)
from ad_remover._segments import (
    AdSegment,
    _merge_overlapping_ads,
    _parse_ad_response,
    _split_segments_into_chunks,
)

logger = logging.getLogger("ad_remover")


def _pkg():
    """Return the live ``ad_remover`` package module (see _ffmpeg._pkg)."""
    return sys.modules["ad_remover"]


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
        response = _pkg().retry_aws_call(
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
        response = _pkg().retry_aws_call(
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
    ``BEDROCK_DETECT_MODEL_ID`` (falling back to ``BEDROCK_MODEL_ID``).

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

    region = settings.get("AWS_DEFAULT_REGION")
    # BEDROCK_DETECT_MODEL_ID overrides BEDROCK_MODEL_ID for detection (first-pass).
    # Use a cheaper model here (e.g. Haiku) and keep Sonnet for verification.
    _verify_model = settings.get("BEDROCK_MODEL_ID")
    model_id = settings.get("BEDROCK_DETECT_MODEL_ID") or _verify_model
    max_chars = settings.get("AD_DETECT_MAX_CHARS")
    overlap_secs = settings.get("AD_DETECT_OVERLAP_SECS")

    chunks = _split_segments_into_chunks(segments, max_chars, overlap_secs)
    logger.info(
        "[AdRemover] Transcript split into %d chunk(s) (max_chars=%d, overlap=%.0fs)",
        len(chunks),
        max_chars,
        overlap_secs,
    )

    bedrock = _pkg().boto3.client("bedrock-runtime", region_name=region)
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

        response = _pkg().retry_aws_call(
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
    _MIN_AD_SECONDS = settings.get("MIN_AD_SEGMENT_SECS")
    max_ad_secs = settings.get("MAX_AD_SEGMENT_SECS")
    verify_threshold = settings.get("AD_VERIFY_THRESHOLD_SECS")

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
            narrowed = _narrow_oversized_segment(seg, segments, bedrock, _verify_model)
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
                if _verify_ad_segment(seg, segments, bedrock, _verify_model):
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
