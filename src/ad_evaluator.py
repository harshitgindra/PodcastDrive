"""Ad removal quality evaluator.

After :func:`~ad_remover.remove_ads` cleans a podcast episode, this module
re-transcribes the cleaned file and checks it for residual ad content using the
same AWS Transcribe + Bedrock pipeline.  Results are written as a JSON report to
``reports/{slug}/{episode_id}_eval.json``.

Environment variables:
    EVALUATE_AD_REMOVAL  – Set to "true" to enable evaluation (default: "false").
                           Evaluation is opt-in to avoid incurring AWS costs on
                           every sync run.
    EVAL_REPORTS_DIR     – Directory to write report files (default: "reports").
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Classification constants
RESULT_CLEAN = "clean"       # No residual ads detected
RESULT_PARTIAL = "partial"   # Residual at boundary of a removed segment (trim miss)
RESULT_MISSED = "missed"     # Residual falls outside all originally removed segments


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_residual(
    residual: dict,
    original_segments: list[dict],
    boundary_tolerance: float = 10.0,
) -> str:
    """Classify a single residual ad segment against the originally removed segments.

    Args:
        residual:            A ``{"start": float, "end": float}`` dict found in the
                             cleaned file.
        original_segments:   The ``[{"start", "end"}]`` list that ``remove_ads``
                             detected and spliced out.
        boundary_tolerance:  Seconds within which a residual is considered a
                             boundary miss rather than a full miss.

    Returns:
        ``"partial"`` if the residual overlaps with or is within *boundary_tolerance*
        seconds of any original segment boundary; ``"missed"`` otherwise.
    """
    r_start = residual["start"]
    r_end = residual["end"]

    for seg in original_segments:
        # Overlap or near-boundary
        if (r_start <= seg["end"] + boundary_tolerance and
                r_end >= seg["start"] - boundary_tolerance):
            return RESULT_PARTIAL

    return RESULT_MISSED


def _build_proposals(
    residuals: list[dict],
    original_segments: list[dict],
) -> list[dict[str, Any]]:
    """Generate human-readable improvement proposals for each residual.

    Args:
        residuals:         Residual ad segments found in the cleaned file.
                           Each dict has ``start``, ``end``, and optionally ``text``.
        original_segments: Segments that were originally detected and removed.

    Returns:
        List of proposal dicts with ``type``, ``affected_segment`` (if applicable),
        and ``suggestion`` string.
    """
    proposals: list[dict[str, Any]] = []

    for residual in residuals:
        classification = _classify_residual(residual, original_segments)
        r_start = residual["start"]
        r_end = residual["end"]
        r_text = residual.get("text", "")

        if classification == RESULT_PARTIAL:
            # Find the closest original segment
            closest = min(
                original_segments,
                key=lambda s: min(abs(s["end"] - r_start), abs(s["start"] - r_end)),
            )
            gap = round(max(0.0, r_start - closest["end"], closest["start"] - r_end), 1)
            suggestions_seconds = max(4, int(gap) + 2)
            proposals.append({
                "type": "boundary_extension",
                "affected_segment": closest,
                "residual": {"start": r_start, "end": r_end},
                "suggestion": (
                    f"Extend segment boundary padding from 2s to ~{suggestions_seconds}s "
                    f"— residual found at [{r_start:.1f}–{r_end:.1f}], "
                    f"original segment ended at {closest['end']:.1f}s. "
                    "Update the '±2 seconds' padding rule in the Bedrock prompt "
                    "(_AD_DETECTION_PROMPT in ad_remover.py)."
                ),
            })

        else:  # RESULT_MISSED
            phrase = r_text[:100].strip() if r_text else "(no transcript text)"
            proposals.append({
                "type": "missed_detection",
                "residual": {"start": r_start, "end": r_end},
                "suggestion": (
                    f"Ad segment [{r_start:.1f}–{r_end:.1f}] was not detected in the "
                    f"first pass. Transcript snippet: '{phrase}'. "
                    "Consider adding matching keywords/phrases to the 'Common ad signals' "
                    "section of _AD_DETECTION_PROMPT in ad_remover.py, or reduce "
                    "AD_DETECT_OVERLAP_SECS to improve chunk boundary coverage."
                ),
            })

    return proposals


# ---------------------------------------------------------------------------
# Fix #1 – Timestamp coordinate translation
# ---------------------------------------------------------------------------

def _translate_cleaned_to_original(
    cleaned_time: float,
    removed_segments: list[dict],
) -> float:
    """Translate a cleaned-file timestamp back to original-file space.

    When ad segments are spliced out, all subsequent content shifts earlier.
    This reverses the shift so residual timestamps can be compared against the
    original-file ad-segment list.

    Args:
        cleaned_time:     Timestamp in the cleaned file (seconds).
        removed_segments: Segments removed during ad splicing (``{start, end}``).

    Returns:
        Equivalent timestamp in the original file (seconds).
    """
    if not removed_segments:
        return cleaned_time

    sorted_segs = sorted(removed_segments, key=lambda s: s["start"])
    cleaned_cursor = 0.0
    original_cursor = 0.0

    for seg in sorted_segs:
        keep_duration = max(0.0, seg["start"] - original_cursor)
        if cleaned_cursor + keep_duration >= cleaned_time:
            # cleaned_time lands inside this keep interval
            return original_cursor + (cleaned_time - cleaned_cursor)
        cleaned_cursor += keep_duration
        original_cursor = seg["end"]

    # cleaned_time is past all removed segments
    return original_cursor + (cleaned_time - cleaned_cursor)


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def evaluate_ad_removal(
    cleaned_mp3: str,
    episode_id: str,
    slug: str,
    original_ad_segments: list[dict] | None = None,
    reports_dir: str | None = None,
) -> dict:
    """Evaluate ad-removal quality by re-transcribing the cleaned file.

    Re-runs ``transcribe_audio`` + ``detect_ads`` on the already-cleaned file.
    Any ads found are residuals — they were missed or incompletely removed.
    Writes a JSON report and returns the report dict.

    Evaluation is gated by the ``EVALUATE_AD_REMOVAL`` environment variable
    (must be ``"true"`` to run).  Returns an empty ``{"skipped": True}`` dict
    when the gate is not set.

    Args:
        cleaned_mp3:          Local path to the ad-cleaned MP3 file.
        episode_id:           Unique episode identifier (used to name the report).
        slug:                 Podcast slug (sub-folder within ``reports_dir``).
        original_ad_segments: Ad segments detected in the original pass — used
                              to classify residuals as boundary misses vs. full
                              misses.  Pass ``None`` or ``[]`` when unavailable.
        reports_dir:          Base directory to write reports into.  Defaults to
                              the ``EVAL_REPORTS_DIR`` env var, then ``"reports"``.

    Returns:
        Report dict with keys: ``episode_id``, ``podcast_slug``, ``evaluated_at``,
        ``result``, ``original_ad_segments``, ``residual_ad_segments``,
        ``total_removed_seconds``, ``residual_seconds``, ``proposals``.
        Returns ``{"skipped": True}`` when evaluation is disabled.
    """
    if os.environ.get("EVALUATE_AD_REMOVAL", "false").lower() not in ("true", "1", "yes"):
        logger.debug("[AdEvaluator] EVALUATE_AD_REMOVAL not set — skipping evaluation for %s", episode_id)
        return {"skipped": True}

    if original_ad_segments is None:
        original_ad_segments = []

    if reports_dir is None:
        reports_dir = os.environ.get("EVAL_REPORTS_DIR", "reports")

    logger.info("[AdEvaluator] Evaluating ad removal quality for %s", episode_id)

    # Import here to avoid circular deps and keep optional dependency clear
    from ad_remover import detect_ads, transcribe_audio

    # --- Step 1: Re-transcribe the cleaned file ---
    try:
        segments = transcribe_audio(cleaned_mp3, f"eval-{episode_id}")
    except Exception as exc:
        logger.warning("[AdEvaluator] Transcription of cleaned file failed for %s: %s", episode_id, exc)
        return {"skipped": True, "error": str(exc)}

    # --- Step 2: Detect residual ads ---
    try:
        residual_segments = detect_ads(segments)
    except Exception as exc:
        logger.warning("[AdEvaluator] Ad detection on cleaned file failed for %s: %s", episode_id, exc)
        return {"skipped": True, "error": str(exc)}

    # Fix #1: translate residual timestamps from cleaned-file space to original-file
    # space so _classify_residual compares apples-to-apples.
    for residual in residual_segments:
        residual["original_time_start"] = round(
            _translate_cleaned_to_original(residual["start"], original_ad_segments), 2
        )
        residual["original_time_end"] = round(
            _translate_cleaned_to_original(residual["end"], original_ad_segments), 2
        )

    # Attach transcript text to residual segments for better proposals
    for residual in residual_segments:
        covered_text = " ".join(
            s["text"] for s in segments
            if s["start"] >= residual["start"] - 2 and s["end"] <= residual["end"] + 2
        )
        residual["text"] = covered_text[:300]

    # --- Step 3: Classify and build proposals (using original-space timestamps) ---
    # Build translated copies for classification so proposals show original times.
    translated_residuals = [
        {**r, "start": r["original_time_start"], "end": r["original_time_end"]}
        for r in residual_segments
    ]
    if not residual_segments:
        result = RESULT_CLEAN
        proposals: list[dict] = []
    else:
        classifications = [
            _classify_residual(r, original_ad_segments) for r in translated_residuals
        ]
        # Overall result is the worst classification found
        result = RESULT_MISSED if RESULT_MISSED in classifications else RESULT_PARTIAL
        proposals = _build_proposals(translated_residuals, original_ad_segments)

    total_removed = sum(s["end"] - s["start"] for s in original_ad_segments)
    residual_secs = sum(s["end"] - s["start"] for s in residual_segments)

    report: dict[str, Any] = {
        "episode_id": episode_id,
        "podcast_slug": slug,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "result": result,
        "original_ad_segments": original_ad_segments,
        "residual_ad_segments": residual_segments,
        "total_removed_seconds": round(total_removed, 2),
        "residual_seconds": round(residual_secs, 2),
        "proposals": proposals,
    }

    # --- Step 4: Write report ---
    try:
        out_dir = os.path.join(reports_dir, slug)
        os.makedirs(out_dir, exist_ok=True)
        report_path = os.path.join(out_dir, f"{episode_id}_eval.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        logger.info(
            "[AdEvaluator] %s — result=%s residuals=%d (%.1fs) → %s",
            episode_id, result, len(residual_segments), residual_secs, report_path,
        )
    except OSError as exc:
        logger.warning("[AdEvaluator] Could not write report for %s: %s", episode_id, exc)

    return report
