"""Pure helpers for ad-segment math and model-response parsing.

Everything here is a deterministic function of its inputs plus the ``settings``
registry — no AWS, ffmpeg or network calls — so these are the functions that
are cheapest to unit-test and safest to reuse. They are imported back into the
:mod:`ad_remover` package namespace by ``__init__`` so existing callers and
tests continue to reference them as ``ad_remover.<name>``.
"""

from __future__ import annotations

import json
import logging
import math

import settings

logger = logging.getLogger("ad_remover")

#: Internal type alias. An ad segment is ``{"start": float, "end": float}``.
AdSegment = dict


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

    merge_gap = settings.get("AD_MERGE_GAP_SECS")
    sorted_ads = sorted(ads, key=lambda s: s["start"])
    merged: list[AdSegment] = [{"start": sorted_ads[0]["start"], "end": sorted_ads[0]["end"]}]

    for seg in sorted_ads[1:]:
        if seg["start"] <= merged[-1]["end"] + merge_gap:
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
        else:
            merged.append({"start": seg["start"], "end": seg["end"]})

    return merged
