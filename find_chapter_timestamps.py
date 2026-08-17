#!/usr/bin/env python3
"""
find_chapter_timestamps.py — Transcribe an audiobook and detect chapter timestamps.

Pipeline:
    1. Upload the MP3 to S3 and transcribe with AWS Transcribe (reuses the
       ad_remover infrastructure — caching included).
    2. Keyword pass: scan the transcript for "Chapter N" / "Part N" / "Prologue"
       patterns. Covers most narrated audiobooks with zero LLM cost.
    3. Bedrock fallback: if the keyword pass finds fewer than 2 chapters, send
       the transcript to Claude and ask it to identify chapter boundaries.
    4. Write chapters.csv (title,start_time) ready for split_audiobook.py.

Requirements:
    S3_BUCKET          — existing PodcastDrive bucket (same as ad removal)
    AWS_DEFAULT_REGION — default: us-east-1
    BEDROCK_MODEL_ID   — default: us.anthropic.claude-sonnet-4-6

Usage:
    python3 find_chapter_timestamps.py <input.mp3> [--output chapters.csv] [--bedrock-only]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Import from src/ ──────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent / "src"))
from ad_remover import transcribe_audio  # noqa: E402  (local import after path fix)

# AWS Transcribe hard limit is 8 hours; we stay safely under it
_TRANSCRIBE_MAX_HOURS = 7.5
_TRANSCRIBE_MAX_SECS = _TRANSCRIBE_MAX_HOURS * 3600  # 27 000 s


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_time(seconds: float) -> str:
    """Convert float seconds → HH:MM:SS.mmm string."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _get_audio_duration(path: str) -> float:
    """Return the total duration of *path* in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr}")
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def _transcribe_chunked(mp3_path: str, book_id: str) -> list[dict]:
    """Transcribe *mp3_path*, automatically chunking files that exceed the
    AWS Transcribe 8-hour limit.

    For files ≤ 7.5 hours the file is submitted as a single job (original
    behaviour).  For longer files the audio is split into ≤ 7.5-hour chunks
    with ``ffmpeg -c copy`` (fast stream copy, no re-encode), each chunk is
    transcribed individually, segment timestamps are offset by the chunk's
    start time, and all segments are merged and returned sorted by time.

    Temporary chunk files are deleted whether the transcription succeeds or
    fails.
    """
    duration = _get_audio_duration(mp3_path)

    # Fast path — file is within the Transcribe limit
    if duration <= _TRANSCRIBE_MAX_SECS:
        return transcribe_audio(mp3_path, book_id)

    # ── Long file: split into chunks ─────────────────────────────────────────
    n_chunks = int(duration / _TRANSCRIBE_MAX_SECS) + 1
    print(
        f"      File is {duration / 3600:.1f} h — exceeds the AWS Transcribe "
        f"8-hour limit.\n"
        f"      Splitting into {n_chunks} chunks of ≤ {_TRANSCRIBE_MAX_HOURS} h each..."
    )

    all_segments: list[dict] = []
    chunk_start = 0.0

    for chunk_idx in range(n_chunks):
        chunk_end = min(chunk_start + _TRANSCRIBE_MAX_SECS, duration)
        chunk_id = f"{book_id}-chunk{chunk_idx + 1}of{n_chunks}"

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp3", prefix=f"audiobook_chunk{chunk_idx}_")
        os.close(tmp_fd)

        try:
            print(
                f"\n      Chunk {chunk_idx + 1}/{n_chunks}: "
                f"{_fmt_time(chunk_start)} → {_fmt_time(chunk_end)} "
                f"({(chunk_end - chunk_start) / 3600:.1f} h)"
            )

            # Extract the chunk with ffmpeg (stream copy — very fast)
            cmd = [
                "ffmpeg", "-y",
                "-i", mp3_path,
                "-ss", str(chunk_start),
                "-to", str(chunk_end),
                "-c", "copy",
                tmp_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg chunk extraction failed (rc={proc.returncode}): "
                    f"{proc.stderr[-500:]}"
                )

            print(f"      Transcribing chunk {chunk_idx + 1} (job id: {chunk_id})...")
            chunk_segments = transcribe_audio(tmp_path, chunk_id)

            # Offset every segment's timestamps by this chunk's start position
            for seg in chunk_segments:
                seg["start"] += chunk_start
                seg["end"] += chunk_start
            all_segments.extend(chunk_segments)
            print(
                f"      Chunk {chunk_idx + 1} done — "
                f"{len(chunk_segments)} segments"
            )

        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        chunk_start = chunk_end
        if chunk_start >= duration:
            break

    all_segments.sort(key=lambda s: s["start"])
    print(f"\n      All chunks transcribed — {len(all_segments)} total segments.")
    return all_segments


def _segments_to_text(segments: list[dict], include_timestamps: bool = True) -> str:
    """Flatten segment list to a readable transcript string."""
    lines = []
    for s in segments:
        if include_timestamps:
            lines.append(f"[{s['start']:.1f}]  {s['text']}")
        else:
            lines.append(s["text"])
    return "\n".join(lines)


# ── Step 2: Keyword-based chapter detection ───────────────────────────────────

# Roman numerals up to 50 (covers most audiobooks)
_ROMAN = (
    "L|XL|XXX|XX|XIX|XVIII|XVII|XVI|XV|XIV|XIII|XII|XI|X|"
    "IX|VIII|VII|VI|V|IV|III|II|I"
)

# Words for numbers 1-30 (spelled out)
_WORD_NUMS = (
    "thirty|twenty.?nine|twenty.?eight|twenty.?seven|twenty.?six|twenty.?five|"
    "twenty.?four|twenty.?three|twenty.?two|twenty.?one|twenty|nineteen|eighteen|"
    "seventeen|sixteen|fifteen|fourteen|thirteen|twelve|eleven|ten|nine|eight|"
    "seven|six|five|four|three|two|one|first|second|third|fourth|fifth|"
    "sixth|seventh|eighth|ninth|tenth"
)

# Patterns that signal a chapter/section heading when spoken
_CHAPTER_PATTERNS = [
    # "Chapter 12", "Chapter Twelve", "Chapter XII"
    rf"\bchapter\s+(?:\d+|{_ROMAN}|{_WORD_NUMS})\b",
    # "Part 3", "Part Three", "Part III"
    rf"\bpart\s+(?:\d+|{_ROMAN}|{_WORD_NUMS})\b",
    # Stand-alone structural sections
    r"\b(?:prologue|epilogue|introduction|preface|afterword|foreword|"
    r"acknowledgements?|appendix|interlude|coda|conclusion)\b",
]

_CHAPTER_RE = re.compile(
    "|".join(_CHAPTER_PATTERNS),
    re.IGNORECASE,
)

# How many seconds before the first matched word to start the chapter
# (gives a tiny lead-in before the narrator says "Chapter")
_LEAD_IN_SECS = 0.5

# Minimum gap between two chapter starts (avoids double-counting "Chapter" + "One")
_MIN_CHAPTER_GAP_SECS = 30.0


def keyword_detect_chapters(segments: list[dict]) -> list[dict]:
    """Scan transcript segments for chapter-heading patterns.

    Returns a list of {"title": str, "start": float} dicts, sorted by start.
    """
    chapters: list[dict] = []
    last_chapter_time: float = -_MIN_CHAPTER_GAP_SECS - 1

    for seg in segments:
        text = seg["text"]
        match = _CHAPTER_RE.search(text)
        if not match:
            continue

        start = max(0.0, seg["start"] - _LEAD_IN_SECS)
        if start - last_chapter_time < _MIN_CHAPTER_GAP_SECS:
            # Same heading repeated in consecutive segments — skip
            continue

        # Clean up title: take the matched phrase, title-case it
        title = match.group(0).strip()
        title = re.sub(r"\s+", " ", title).title()

        chapters.append({"title": title, "start": start})
        last_chapter_time = start

    return chapters


# ── Step 3: Bedrock fallback ──────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are an expert audiobook editor. "
    "Output ONLY valid JSON. No prose, no markdown, no commentary outside the JSON."
)

_DETECT_PROMPT = """\
Below is a timestamped transcript of an audiobook (timestamps are seconds from start).
Identify every chapter or major section boundary in the transcript.

For each chapter, return:
  - "title": the chapter name or number as spoken (e.g. "Chapter One", "Prologue")
  - "start": the start time in seconds (float) of the first word of that chapter heading

Return a JSON array sorted by start time. If you cannot find clear chapter boundaries,
return an empty array [].

TRANSCRIPT:
{transcript}

Return ONLY a JSON array: [{{"title": "...", "start": 123.4}}, ...]
"""

_CHUNK_MAX_CHARS = 80_000   # ~60k words — well within Claude's context
_CHUNK_OVERLAP_SECS = 120   # 2-minute overlap between chunks


def _bedrock_detect_chapters(transcript_text: str, region: str, model_id: str) -> list[dict]:
    """Send transcript_text to Bedrock and parse the chapter list."""
    import boto3

    bedrock = boto3.client("bedrock-runtime", region_name=region)
    prompt = _DETECT_PROMPT.format(transcript=transcript_text[:_CHUNK_MAX_CHARS])

    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": _SYSTEM_PROMPT}],
        messages=[
            {"role": "user", "content": [{"text": prompt}]},
            {"role": "assistant", "content": [{"text": "["}]},
        ],
        inferenceConfig={"temperature": 0.0, "maxTokens": 4096},
    )
    raw = "[" + response["output"]["message"]["content"][0]["text"].strip()

    # Extract the JSON array
    start_idx = raw.find("[")
    end_idx = raw.rfind("]") + 1
    if start_idx == -1 or end_idx == 0:
        print("  Bedrock returned no JSON array.", file=sys.stderr)
        return []

    data = json.loads(raw[start_idx:end_idx])
    return [
        {"title": str(d.get("title", "")).strip(), "start": float(d["start"])}
        for d in data
        if "start" in d
    ]


def _bedrock_detect_chapters_chunked(
    segments: list[dict],
    region: str,
    model_id: str,
) -> list[dict]:
    """For very long audiobooks, chunk the transcript and merge results."""
    full_text = _segments_to_text(segments)

    if len(full_text) <= _CHUNK_MAX_CHARS:
        print("  Sending full transcript to Bedrock...")
        return _bedrock_detect_chapters(full_text, region, model_id)

    # Split into time-based chunks with overlap
    print(f"  Transcript is {len(full_text):,} chars — chunking for Bedrock...")
    total_duration = segments[-1]["end"] if segments else 0.0
    chunk_duration = total_duration * (_CHUNK_MAX_CHARS / len(full_text))

    all_chapters: list[dict] = []
    seen_starts: set[float] = set()
    chunk_start = 0.0

    chunk_idx = 0
    while chunk_start < total_duration:
        chunk_end = chunk_start + chunk_duration
        chunk_segs = [
            s for s in segments
            if s["start"] >= chunk_start and s["start"] < chunk_end + _CHUNK_OVERLAP_SECS
        ]
        if not chunk_segs:
            break

        chunk_text = _segments_to_text(chunk_segs)
        chunk_idx += 1
        print(f"  Chunk {chunk_idx}: {chunk_start:.0f}s–{chunk_end:.0f}s ({len(chunk_text):,} chars)")
        chapters = _bedrock_detect_chapters(chunk_text, region, model_id)

        for ch in chapters:
            # Deduplicate by rounding to nearest 5s
            rounded = round(ch["start"] / 5) * 5
            if rounded not in seen_starts:
                seen_starts.add(rounded)
                all_chapters.append(ch)

        chunk_start = chunk_end

    all_chapters.sort(key=lambda c: c["start"])
    return all_chapters


# ── Step 4: Write CSV ─────────────────────────────────────────────────────────

def write_chapters_csv(chapters: list[dict], output_path: str) -> None:
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "start_time"])
        writer.writeheader()
        for ch in chapters:
            writer.writerow({"title": ch["title"], "start_time": _fmt_time(ch["start"])})
    print(f"\nWrote {len(chapters)} chapters → {output_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Detect audiobook chapter timestamps via transcription")
    parser.add_argument("input", help="Input MP3 file")
    parser.add_argument("--output", "-o", default=None, help="Output CSV path (default: <input>_chapters.csv)")
    parser.add_argument("--bedrock-only", action="store_true", help="Skip keyword detection and go straight to Bedrock")
    parser.add_argument("--save-transcript", action="store_true", help="Save the raw transcript to <input>_transcript.txt")
    args = parser.parse_args()

    mp3_path = args.input
    if not Path(mp3_path).exists():
        print(f"File not found: {mp3_path}", file=sys.stderr)
        sys.exit(1)

    output_csv = args.output or (Path(mp3_path).stem + "_chapters.csv")
    book_id = re.sub(r"[^A-Za-z0-9_-]", "-", Path(mp3_path).stem)[:60]

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

    # ── Step 1: Transcribe ────────────────────────────────────────────────────
    print(f"\n[1/3] Transcribing {mp3_path} ...")
    print("      (AWS Transcribe — may take several minutes for long files)")
    print(f"      Book ID: {book_id}  |  Region: {region}")

    try:
        segments = _transcribe_chunked(mp3_path, book_id)
    except RuntimeError as exc:
        print(f"\nTranscription failed: {exc}", file=sys.stderr)
        print("\nMake sure S3_BUCKET is set in config.env and AWS credentials are active.", file=sys.stderr)
        sys.exit(1)

    if not segments:
        print("Transcription returned no segments.", file=sys.stderr)
        sys.exit(1)

    print(f"      Got {len(segments)} transcript segments.")
    total_dur = segments[-1]["end"]
    print(f"      Duration: {_fmt_time(total_dur)}")

    if args.save_transcript:
        txt_path = Path(mp3_path).stem + "_transcript.txt"
        Path(txt_path).write_text(_segments_to_text(segments))
        print(f"      Transcript saved → {txt_path}")

    # ── Step 2: Keyword detection ─────────────────────────────────────────────
    chapters: list[dict] = []

    if not args.bedrock_only:
        print("\n[2/3] Scanning transcript for chapter keywords ...")
        chapters = keyword_detect_chapters(segments)
        if chapters:
            print(f"      Found {len(chapters)} chapter(s) via keyword matching:")
            for i, ch in enumerate(chapters, 1):
                print(f"        {i:>3}. [{_fmt_time(ch['start'])}]  {ch['title']}")
        else:
            print("      No keyword matches found.")

    # ── Step 3: Bedrock fallback ──────────────────────────────────────────────
    if len(chapters) < 2:
        print(f"\n[3/3] Falling back to Bedrock ({model_id}) for chapter detection ...")
        try:
            chapters = _bedrock_detect_chapters_chunked(segments, region, model_id)
            if chapters:
                print(f"      Bedrock found {len(chapters)} chapter(s):")
                for i, ch in enumerate(chapters, 1):
                    print(f"        {i:>3}. [{_fmt_time(ch['start'])}]  {ch['title']}")
            else:
                print("      Bedrock found no chapter boundaries either.")
                print("\nSuggestion: run with --save-transcript, open the .txt file,")
                print("and manually create chapters.csv (title,start_time).")
                sys.exit(1)
        except Exception as exc:
            print(f"\nBedrock call failed: {exc}", file=sys.stderr)
            if args.save_transcript:
                print(f"The transcript was saved to {Path(mp3_path).stem}_transcript.txt — review it manually.", file=sys.stderr)
            sys.exit(1)
    else:
        print("\n[3/3] Keyword detection sufficient — skipping Bedrock.")

    # ── Step 4: Write CSV ─────────────────────────────────────────────────────
    write_chapters_csv(chapters, output_csv)
    print("\nNext step:")
    print(f"  python3 split_audiobook.py \"{mp3_path}\" --chapters \"{output_csv}\"")


if __name__ == "__main__":
    main()
