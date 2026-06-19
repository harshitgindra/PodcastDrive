#!/usr/bin/env python3
"""Ad detection evaluation harness.

Downloads (or uses cached) episodes, transcribes them, runs ad detection,
and outputs detailed results for manual review or scoring against ground truth.

Usage:
    # Transcribe + detect ads for all episodes in eval/episodes/
    python eval/run_eval.py

    # Score against ground truth (after you create ground_truth.json)
    python eval/run_eval.py --score

    # Skip transcription (reuse cached transcripts)
    python eval/run_eval.py --skip-transcribe

    # Use a specific model for ad detection
    python eval/run_eval.py --model us.anthropic.claude-opus-4-20250514-v1:0

    # Run silence detection analysis
    python eval/run_eval.py --silence
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ad_remover import detect_ads, splice_audio, transcribe_audio

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("eval")

EVAL_DIR = Path(__file__).parent
EPISODES_DIR = EVAL_DIR / "episodes"
TRANSCRIPTS_DIR = EVAL_DIR / "transcripts"
RESULTS_DIR = EVAL_DIR / "results"
GROUND_TRUTH_FILE = EVAL_DIR / "ground_truth.json"


def get_episode_files() -> list[Path]:
    """Return all MP3 files in the episodes directory."""
    return sorted(EPISODES_DIR.glob("*.mp3"))


def get_audio_duration(mp3_path: str) -> float:
    """Get duration of an audio file via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", mp3_path],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def detect_silence(mp3_path: str, noise_threshold: str = "-35dB",
                   min_duration: float = 0.5) -> list[dict]:
    """Detect silence intervals in an audio file using ffmpeg.

    Returns list of {"start": float, "end": float, "duration": float} dicts.
    """
    cmd = [
        "ffmpeg", "-i", mp3_path,
        "-af", f"silencedetect=noise={noise_threshold}:d={min_duration}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr

    silences = []
    current_start = None
    for line in stderr.split("\n"):
        if "silence_start:" in line:
            try:
                current_start = float(line.split("silence_start:")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif "silence_end:" in line and current_start is not None:
            try:
                parts = line.split("silence_end:")[1].strip().split("|")
                end = float(parts[0].strip().split()[0])
                duration = float(parts[1].split("silence_duration:")[1].strip()) if len(parts) > 1 else end - current_start
                silences.append({
                    "start": round(current_start, 2),
                    "end": round(end, 2),
                    "duration": round(duration, 2),
                })
                current_start = None
            except (ValueError, IndexError):
                current_start = None

    return silences


def detect_energy_changes(mp3_path: str, window_sec: float = 2.0) -> list[dict]:
    """Detect significant energy level changes that might indicate ad transitions.

    Uses ffmpeg's astats filter to compute RMS energy in windows.
    Returns timestamps where energy changes significantly.
    """
    cmd = [
        "ffmpeg", "-i", mp3_path,
        "-af", f"asegment=timestamps=0,astats=metadata=1:reset={window_sec}",
        "-f", "null", "-",
    ]
    # This is a simplified approach — just detect long silences as boundaries
    return detect_silence(mp3_path, noise_threshold="-30dB", min_duration=1.0)


def transcribe_episode(mp3_path: Path, skip_if_cached: bool = True) -> list[dict]:
    """Transcribe an episode, caching the result."""
    transcript_path = TRANSCRIPTS_DIR / f"{mp3_path.stem}.json"

    if skip_if_cached and transcript_path.exists():
        logger.info("Using cached transcript: %s", transcript_path.name)
        with open(transcript_path) as f:
            return json.load(f)

    logger.info("Transcribing: %s", mp3_path.name)
    video_id = mp3_path.stem[:64]
    segments = transcribe_audio(str(mp3_path), video_id)

    # Cache the transcript
    with open(transcript_path, "w") as f:
        json.dump(segments, f, indent=2)
    logger.info("Transcript cached: %s (%d segments)", transcript_path.name, len(segments))

    return segments


def run_ad_detection(segments: list[dict], model_id: str | None = None) -> list[dict]:
    """Run ad detection, optionally overriding the model."""
    if model_id:
        original = os.environ.get("BEDROCK_MODEL_ID")
        os.environ["BEDROCK_MODEL_ID"] = model_id

    try:
        return detect_ads(segments)
    finally:
        if model_id:
            if original:
                os.environ["BEDROCK_MODEL_ID"] = original
            else:
                os.environ.pop("BEDROCK_MODEL_ID", None)


def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS."""
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m:02d}:{s:02d}"


def print_detection_report(
    mp3_path: Path,
    segments: list[dict],
    ad_segments: list[dict],
    silences: list[dict] | None = None,
):
    """Print a detailed human-readable report of detections."""
    duration = get_audio_duration(str(mp3_path))

    print(f"\n{'='*70}")
    print(f"Episode: {mp3_path.name}")
    print(f"Duration: {format_timestamp(duration)} ({duration:.0f}s)")
    print(f"Transcript segments: {len(segments)}")
    print(f"{'='*70}")

    if not ad_segments:
        print("\n  ⚠️  NO ADS DETECTED")
        print()
        return

    total_ad_time = sum(s["end"] - s["start"] for s in ad_segments)
    print(f"\nDetected {len(ad_segments)} ad segment(s) — {total_ad_time:.0f}s total "
          f"({total_ad_time/duration*100:.1f}% of episode)")
    print()

    for i, ad in enumerate(ad_segments, 1):
        ad_duration = ad["end"] - ad["start"]
        print(f"  Ad #{i}: {format_timestamp(ad['start'])} → {format_timestamp(ad['end'])} "
              f"({ad_duration:.0f}s)")

        # Show transcript text covered by this ad
        covered = [
            s for s in segments
            if s["end"] >= ad["start"] - 2 and s["start"] <= ad["end"] + 2
        ]
        if covered:
            text = " ".join(s["text"] for s in covered)
            # Show first and last 150 chars
            if len(text) > 320:
                print(f"    Text: {text[:150]}...")
                print(f"    ...{text[-150:]}")
            else:
                print(f"    Text: {text}")
        print()

    # Show nearby silences if available
    if silences:
        print(f"  Silence gaps near ads (potential boundaries):")
        for ad in ad_segments:
            nearby = [s for s in silences
                      if abs(s["start"] - ad["start"]) < 10
                      or abs(s["end"] - ad["end"]) < 10
                      or abs(s["start"] - ad["end"]) < 10
                      or abs(s["end"] - ad["start"]) < 10]
            for sil in nearby:
                print(f"    Silence at {format_timestamp(sil['start'])} → "
                      f"{format_timestamp(sil['end'])} ({sil['duration']:.1f}s)")
        print()


def score_against_ground_truth(results: dict, ground_truth: dict) -> dict:
    """Compute precision, recall, and F1 against human annotations.

    Uses overlap-based matching: a detected segment counts as a true positive
    if it overlaps ≥50% with a ground truth segment (and vice versa for recall).
    """
    scores = {}

    for episode_name, gt_segments in ground_truth.items():
        detected = results.get(episode_name, {}).get("ad_segments", [])

        if not gt_segments and not detected:
            scores[episode_name] = {"precision": 1.0, "recall": 1.0, "f1": 1.0}
            continue

        # True positives: detected segments that overlap ≥50% with a GT segment
        tp = 0
        matched_gt = set()
        for det in detected:
            det_start, det_end = det["start"], det["end"]
            det_len = det_end - det_start
            for j, gt in enumerate(gt_segments):
                gt_start, gt_end = gt["start"], gt["end"]
                overlap_start = max(det_start, gt_start)
                overlap_end = min(det_end, gt_end)
                overlap = max(0, overlap_end - overlap_start)
                if overlap >= 0.5 * det_len or overlap >= 0.5 * (gt_end - gt_start):
                    tp += 1
                    matched_gt.add(j)
                    break

        precision = tp / len(detected) if detected else 0.0
        recall = len(matched_gt) / len(gt_segments) if gt_segments else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        scores[episode_name] = {
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "detected_count": len(detected),
            "ground_truth_count": len(gt_segments),
            "true_positives": tp,
            "missed": len(gt_segments) - len(matched_gt),
            "false_positives": len(detected) - tp,
        }

    return scores



def ci_check_scores(
    scores: dict,
    f1_threshold: float = 0.75,
    recall_threshold: float = 0.70,
) -> tuple[bool, list[str]]:
    """Return (passed, failure_messages) for CI gate."""
    failures = []
    for episode, s in scores.items():
        if s["f1"] < f1_threshold:
            failures.append(
                f"{episode}: F1={s['f1']:.1%} < threshold {f1_threshold:.1%} "
                f"(precision={s['precision']:.1%}, recall={s['recall']:.1%}, "
                f"missed={s.get('missed', '?')}, fp={s.get('false_positives', '?')})"
            )
        elif s["recall"] < recall_threshold:
            failures.append(
                f"{episode}: recall={s['recall']:.1%} < threshold {recall_threshold:.1%} "
                f"(F1={s['f1']:.1%}, missed={s.get('missed', '?')})"
            )
    return (len(failures) == 0, failures)


def check_phrases_absent(
    segments: list[dict],
    phrases: list[str],
    context_window: int = 5,
) -> list[dict]:
    """Check that known ad phrases do NOT appear in transcript segments.

    Case-insensitive substring search. Returns a list of violations.
    """
    if not segments or not phrases:
        return []
    full_text_lower = " ".join(s["text"] for s in segments).lower()
    violations = []
    for phrase in phrases:
        if phrase.lower() in full_text_lower:
            for seg in segments:
                if phrase.lower() in seg["text"].lower():
                    violations.append({
                        "phrase": phrase,
                        "found_at": f"{seg['start']:.1f}s\u2013{seg['end']:.1f}s",
                        "context": seg["text"][:120],
                    })
                    break
    return violations


def check_phrases_present(
    segments: list[dict],
    phrases: list[str],
) -> list[str]:
    """Check that known content phrases ARE present in transcript segments.

    Returns a list of missing phrases — empty list = pass.
    """
    if not phrases:
        return []
    if not segments:
        return list(phrases)
    full_text_lower = " ".join(s["text"] for s in segments).lower()
    return [p for p in phrases if p.lower() not in full_text_lower]


def check_duration_reduction(
    original_path: str,
    cleaned_path: str,
    expected_removed_secs: float,
    tolerance_secs: float = 60.0,
) -> tuple[bool, str]:
    """Validate cleaned file duration is within tolerance of expected reduction."""
    orig_dur = get_audio_duration(original_path)
    clean_dur = get_audio_duration(cleaned_path)
    actual_removed = orig_dur - clean_dur
    deviation = abs(actual_removed - expected_removed_secs)
    summary = (
        f"original={orig_dur:.0f}s  cleaned={clean_dur:.0f}s  "
        f"removed={actual_removed:.0f}s  expected\u2248{expected_removed_secs:.0f}s  "
        f"deviation={deviation:.0f}s  tolerance=\u00b1{tolerance_secs:.0f}s"
    )
    if actual_removed < 0:
        return False, f"Cleaned file is LONGER than original \u2014 splice likely failed \u2014 {summary}"
    if deviation > tolerance_secs:
        return False, f"Duration deviation {deviation:.0f}s exceeds \u00b1{tolerance_secs:.0f}s \u2014 {summary}"
    return True, summary


def main():
    parser = argparse.ArgumentParser(description="Ad detection evaluation harness")
    parser.add_argument("--skip-transcribe", action="store_true",
                        help="Use cached transcripts only (fail if not cached)")
    parser.add_argument("--score", action="store_true",
                        help="Score results against ground_truth.json")
    parser.add_argument("--model", type=str, default=None,
                        help="Override Bedrock model ID for ad detection")
    parser.add_argument("--silence", action="store_true",
                        help="Run silence detection analysis")
    parser.add_argument("--splice", action="store_true",
                        help="Actually splice out ads and save cleaned files")
    parser.add_argument("--episodes", nargs="*", default=None,
                        help="Process only these episode files (by name)")
    parser.add_argument("--ci", action="store_true",
                        help="Exit with code 1 if any fixture fails F1/recall thresholds. Implies --score.")
    parser.add_argument("--f1-threshold", type=float, default=0.75, dest="f1_threshold",
                        help="Minimum F1 score required per fixture in --ci mode (default: 0.75)")
    parser.add_argument("--recall-threshold", type=float, default=0.70, dest="recall_threshold",
                        help="Minimum recall required per fixture in --ci mode (default: 0.70)")
    parser.add_argument("--check-properties", action="store_true", dest="check_properties",
                        help="Run duration-reduction and phrase-presence checks against ground_truth.json. Implies --splice.")
    parser.add_argument("--update-ground-truth", action="store_true", dest="update_ground_truth",
                        help="Write current detection results to ground_truth.json as new baseline.")
    args = parser.parse_args()

    # --ci implies --score; --check-properties implies --splice
    if args.ci:
        args.score = True
    if args.check_properties:
        args.splice = True

    episodes = get_episode_files()
    if not episodes:
        print(f"No MP3 files found in {EPISODES_DIR}")
        print("Download episodes first, e.g.:")
        print("  python eval/download_episodes.py 'The Best One Yet' --count 3")
        sys.exit(1)

    if args.episodes:
        episodes = [e for e in episodes if any(pat in e.name for pat in args.episodes)]

    print(f"Found {len(episodes)} episode(s) in {EPISODES_DIR}")

    all_results = {}

    for mp3_path in episodes:
        # Step 1: Transcribe
        segments = transcribe_episode(mp3_path, skip_if_cached=True)

        if not segments:
            logger.warning("No transcript for %s — skipping", mp3_path.name)
            continue

        # Step 2: Silence detection (optional)
        silences = None
        if args.silence:
            logger.info("Running silence detection on %s", mp3_path.name)
            silences = detect_silence(str(mp3_path))
            silence_path = RESULTS_DIR / f"{mp3_path.stem}_silences.json"
            with open(silence_path, "w") as f:
                json.dump(silences, f, indent=2)

        # Step 3: Ad detection
        logger.info("Running ad detection on %s", mp3_path.name)
        ad_segments = run_ad_detection(segments, model_id=args.model)

        # Step 4: Report
        print_detection_report(mp3_path, segments, ad_segments, silences)

        # Step 5: Save results
        result = {
            "episode": mp3_path.name,
            "duration": get_audio_duration(str(mp3_path)),
            "transcript_segments": len(segments),
            "ad_segments": ad_segments,
            "total_ad_seconds": sum(s["end"] - s["start"] for s in ad_segments),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": args.model or os.environ.get("BEDROCK_MODEL_ID", "default"),
        }
        if silences:
            result["silences"] = silences

        all_results[mp3_path.name] = result

        result_path = RESULTS_DIR / f"{mp3_path.stem}_result.json"
        with open(result_path, "w") as f:
            json.dump(result, f, indent=2)

        # Step 6: Splice (optional)
        if args.splice and ad_segments:
            output_path = RESULTS_DIR / f"{mp3_path.stem}_cleaned.mp3"
            logger.info("Splicing ads from %s → %s", mp3_path.name, output_path.name)
            try:
                splice_audio(str(mp3_path), ad_segments, str(output_path))
                cleaned_duration = get_audio_duration(str(output_path))
                print(f"  ✅ Cleaned file: {output_path.name} "
                      f"({format_timestamp(cleaned_duration)})")
            except Exception as exc:
                print(f"  ❌ Splice failed: {exc}")

        # Property checks: phrase presence/absence + duration reduction
        if args.check_properties and ad_segments and GROUND_TRUTH_FILE.exists():
            with open(GROUND_TRUTH_FILE) as _gtf:
                _gt = json.load(_gtf)
            _ep_gt = _gt.get(mp3_path.name, {})

            # Duration check
            output_path = RESULTS_DIR / f"{mp3_path.stem}_cleaned.mp3"
            if output_path.exists():
                exp_removed = _ep_gt.get("expected_removed_secs", 0.0)
                ok, msg = check_duration_reduction(str(mp3_path), str(output_path), exp_removed)
                print(f"  {'✅' if ok else '❌'}  Duration: {msg}")

            # Phrase checks — use cached cleaned transcript if available
            cleaned_transcript_path = TRANSCRIPTS_DIR / f"{mp3_path.stem}_cleaned.json"
            if cleaned_transcript_path.exists():
                with open(cleaned_transcript_path) as _ctf:
                    cleaned_segs = json.load(_ctf)
                ad_phrases = _ep_gt.get("ad_phrases", [])
                content_phrases = _ep_gt.get("content_phrases", [])
                if ad_phrases:
                    violations = check_phrases_absent(cleaned_segs, ad_phrases)
                    if violations:
                        print(f"  ❌  Ad phrases still present after cleaning:")
                        for v in violations:
                            print(f"       '{v['phrase']}' at {v['found_at']}: {v['context']!r}")
                    else:
                        print(f"  ✅  No ad phrases found in cleaned transcript ({len(ad_phrases)} checked)")
                if content_phrases:
                    missing = check_phrases_present(cleaned_segs, content_phrases)
                    if missing:
                        print(f"  ❌  Content phrases missing from cleaned transcript: {missing}")
                    else:
                        print(f"  ✅  All content phrases present ({len(content_phrases)} checked)")

    # Save combined results
    combined_path = RESULTS_DIR / "all_results.json"
    with open(combined_path, "w") as f:
        json.dump(all_results, f, indent=2)

    scores = {}

    # Score if requested
    if args.score:
        if not GROUND_TRUTH_FILE.exists():
            print(f"\n⚠️  Ground truth file not found: {GROUND_TRUTH_FILE}")
            print("Create it with the format:")
            print(json.dumps({
                "episode_filename.mp3": [
                    {"start": 60.0, "end": 120.0, "label": "sponsor: BetterHelp"},
                    {"start": 600.0, "end": 660.0, "label": "mid-roll: Athletic Greens"},
                ]
            }, indent=2))
            sys.exit(1)

        with open(GROUND_TRUTH_FILE) as f:
            ground_truth_raw = json.load(f)

        # Normalise ground truth: the file uses {episode: {"segments": [...]}}
        # but score_against_ground_truth expects {episode: [segments...]}.
        # Also filter out metadata keys (starting with '_').
        ground_truth = {}
        for key, value in ground_truth_raw.items():
            if key.startswith("_"):
                continue
            if isinstance(value, dict):
                ground_truth[key] = value.get("segments", [])
            else:
                ground_truth[key] = value  # already a list (legacy format)

        scores = score_against_ground_truth(all_results, ground_truth)

        print(f"\n{'='*70}")
        print("SCORING RESULTS")
        print(f"{'='*70}")
        for ep, s in scores.items():
            print(f"\n  {ep}")
            print(f"    Precision: {s['precision']:.1%}  Recall: {s['recall']:.1%}  F1: {s['f1']:.1%}")
            if "missed" in s:
                print(f"    Detected: {s['detected_count']}  GT: {s['ground_truth_count']}  "
                      f"TP: {s['true_positives']}  Missed: {s['missed']}  FP: {s['false_positives']}")

        # Overall
        all_p = [s["precision"] for s in scores.values()]
        all_r = [s["recall"] for s in scores.values()]
        all_f = [s["f1"] for s in scores.values()]
        if all_p:
            print(f"\n  AVERAGE — Precision: {sum(all_p)/len(all_p):.1%}  "
                  f"Recall: {sum(all_r)/len(all_r):.1%}  F1: {sum(all_f)/len(all_f):.1%}")


    # --ci gate: fail if any fixture is below threshold
    if args.ci and args.score:
        passed, failures = ci_check_scores(
            scores, args.f1_threshold, args.recall_threshold
        )
        if not passed:
            print(f"\n{'='*70}")
            print("❌  CI GATE FAILED")
            print(f"{'='*70}")
            for msg in failures:
                print(f"  • {msg}")
            print(f"\nRun without --ci to see full detection report.")
            sys.exit(1)
        else:
            print(f"\n✅  CI gate passed — all fixtures ≥ F1={args.f1_threshold:.0%} / recall={args.recall_threshold:.0%}")

    # --update-ground-truth: write current detections as new ground truth baseline
    if args.update_ground_truth:
        new_gt = {}
        for ep_name, ep_result in all_results.items():
            existing = {}
            if GROUND_TRUTH_FILE.exists():
                with open(GROUND_TRUTH_FILE) as f:
                    existing = json.load(f).get(ep_name, {})
            new_gt[ep_name] = {
                "segments": ep_result["ad_segments"],
                "expected_removed_secs": round(ep_result["total_ad_seconds"], 1),
                "ad_phrases": existing.get("ad_phrases", []),
                "content_phrases": existing.get("content_phrases", []),
                "_note": "Auto-generated from detection run. Review segments before committing.",
            }
        with open(GROUND_TRUTH_FILE, "w") as f:
            json.dump(new_gt, f, indent=2)
        print(f"\n✅  ground_truth.json updated with {sum(len(v['segments']) for v in new_gt.values())} segments across {len(new_gt)} fixtures.")
        print(f"   Review {GROUND_TRUTH_FILE} then commit it.")


if __name__ == "__main__":
    main()
