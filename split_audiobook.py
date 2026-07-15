#!/usr/bin/env python3
"""
split_audiobook.py — Split a single audiobook MP3 into per-chapter files.

Usage:
    python3 split_audiobook.py <input.mp3> [--output-dir <dir>] [--chapters <chapters.csv>]

If the MP3 has embedded chapter markers (ID3 CHAP), they are used automatically.
If not, supply a CSV with columns: title,start_time (e.g. "Chapter 1,00:00:00").

Output files are named: 01 - Chapter Title.mp3
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path


def probe_chapters(input_file: str) -> list[dict]:
    """Extract embedded chapter markers using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_chapters",
            input_file,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ffprobe error: {result.stderr}", file=sys.stderr)
        return []

    data = json.loads(result.stdout)
    chapters = []
    for ch in data.get("chapters", []):
        chapters.append({
            "title": ch.get("tags", {}).get("title", f"Chapter {ch['id'] + 1}"),
            "start": float(ch["start_time"]),
            "end": float(ch["end_time"]),
        })
    return chapters


def load_csv_chapters(csv_file: str, total_duration: float) -> list[dict]:
    """Load chapter timestamps from a CSV file (title, start_time columns)."""
    chapters = []
    with open(csv_file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = row["title"].strip()
            start = parse_time(row["start_time"].strip())
            chapters.append({"title": title, "start": start})

    # Compute end times: each chapter ends where the next begins
    for i, ch in enumerate(chapters):
        ch["end"] = chapters[i + 1]["start"] if i + 1 < len(chapters) else total_duration

    return chapters


def parse_time(time_str: str) -> float:
    """Parse HH:MM:SS or MM:SS or raw seconds into float seconds."""
    if re.match(r"^\d+(\.\d+)?$", time_str):
        return float(time_str)
    parts = list(map(float, time_str.replace(",", ".").split(":")))
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    raise ValueError(f"Cannot parse time: {time_str}")


def get_duration(input_file: str) -> float:
    """Get total duration of audio file in seconds."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            input_file,
        ],
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def safe_filename(title: str) -> str:
    """Sanitize chapter title for use as a filename."""
    return re.sub(r'[<>:"/\\|?*]', "", title).strip()


def split_chapter(input_file: str, output_path: Path, start: float, end: float):
    """Extract a single chapter using ffmpeg (stream copy, no re-encode)."""
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", input_file,
            "-ss", str(start),
            "-to", str(end),
            "-c", "copy",
            "-map_chapters", "-1",  # strip chapter markers from individual files
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr[-500:]}", file=sys.stderr)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Split audiobook MP3 into chapters")
    parser.add_argument("input", help="Input MP3 file")
    parser.add_argument("--output-dir", "-o", default=None, help="Output directory (default: <input>_chapters/)")
    parser.add_argument("--chapters", "-c", default=None, help="CSV file with title,start_time columns")
    args = parser.parse_args()

    input_file = args.input
    if not Path(input_file).exists():
        print(f"File not found: {input_file}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else Path(input_file).stem + "_chapters"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load chapters
    if args.chapters:
        print(f"Loading chapters from {args.chapters}...")
        total_duration = get_duration(input_file)
        chapters = load_csv_chapters(args.chapters, total_duration)
    else:
        print("Probing for embedded chapters...")
        chapters = probe_chapters(input_file)

    if not chapters:
        print("\nNo embedded chapters found.")
        print("Create a CSV with columns 'title,start_time' and pass it via --chapters.")
        print("\nExample chapters.csv:")
        print("  title,start_time")
        print("  Introduction,00:00:00")
        print("  Chapter 1,00:05:30")
        print("  Chapter 2,00:45:12")
        sys.exit(1)

    print(f"\nFound {len(chapters)} chapters → {output_dir}/\n")

    for i, ch in enumerate(chapters, start=1):
        name = f"{i:02d} - {safe_filename(ch['title'])}.mp3"
        out_path = output_dir / name
        duration_min = (ch["end"] - ch["start"]) / 60
        print(f"  [{i:02d}/{len(chapters)}] {name}  ({duration_min:.1f} min)", end=" ... ", flush=True)
        ok = split_chapter(input_file, out_path, ch["start"], ch["end"])
        print("✓" if ok else "FAILED")

    print(f"\nDone. Files written to: {output_dir}/")


if __name__ == "__main__":
    main()
