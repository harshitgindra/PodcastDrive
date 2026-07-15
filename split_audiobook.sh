#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# split_audiobook.sh — Transcribe an audiobook MP3 and split it into chapters
#
# Usage:
#   ./split_audiobook.sh audiobook.mp3
#   ./split_audiobook.sh audiobook.mp3 --output-dir ~/audiobooks/split
#   ./split_audiobook.sh audiobook.mp3 --chapters existing_chapters.csv
#   ./split_audiobook.sh audiobook.mp3 --bedrock-only
#   ./split_audiobook.sh audiobook.mp3 --save-transcript
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()      { echo -e "  ${GREEN}✅  $*${RESET}"; }
fail()    { echo -e "\n  ${RED}❌  $*${RESET}\n"; exit 1; }
warn()    { echo -e "  ${YELLOW}⚠️   $*${RESET}"; }
info()    { echo -e "  ${CYAN}ℹ️   $*${RESET}"; }
section() { echo -e "\n${BOLD}$*${RESET}\n$(printf '─%.0s' {1..52})"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Help ──────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--help" ]] || [[ "${1:-}" == "-h" ]]; then
    cat << 'HELPEOF'
PodcastDrive — split_audiobook.sh

Transcribe an audiobook MP3, detect chapter boundaries, and split into
one MP3 per chapter. Uses AWS Transcribe + Bedrock (same setup as the
ad-removal pipeline).

Usage:
  ./split_audiobook.sh <audiobook.mp3> [OPTIONS]

Options:
  --help, -h             Show this help message and exit
  --output-dir DIR       Directory for split chapter files (default: <name>_chapters/)
  --chapters FILE        Skip transcription — use an existing chapters.csv instead
  --bedrock-only         Skip keyword detection; send full transcript to Bedrock
  --save-transcript      Save the raw timestamped transcript to <name>_transcript.txt

Environment:
  Reads config.env for S3_BUCKET, AWS_DEFAULT_REGION, BEDROCK_MODEL_ID, etc.

Chapter detection strategy:
  1. AWS Transcribe produces a word-level timestamped transcript (cached in S3).
  2. A keyword pass scans for "Chapter N / Part N / Prologue / Epilogue" patterns.
  3. If fewer than 2 chapters are found, Bedrock (Claude) analyses the transcript.
  4. The resulting chapters.csv is fed to split_audiobook.py for the actual split.

Examples:
  ./split_audiobook.sh "The Great Gatsby.mp3"
  ./split_audiobook.sh book.mp3 --output-dir ~/Desktop/chapters
  ./split_audiobook.sh book.mp3 --chapters my_chapters.csv
  ./split_audiobook.sh book.mp3 --save-transcript --bedrock-only
HELPEOF
    exit 0
fi

# ── Args ──────────────────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
    fail "Usage: ./split_audiobook.sh <audiobook.mp3> [OPTIONS]\nRun with --help for details."
fi

INPUT_FILE="$1"; shift

OUTPUT_DIR=""
CHAPTERS_CSV=""
EXTRA_DETECT_FLAGS=()
EXTRA_SPLIT_FLAGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output-dir)   OUTPUT_DIR="$2"; shift 2 ;;
        --chapters)     CHAPTERS_CSV="$2"; shift 2 ;;
        --bedrock-only) EXTRA_DETECT_FLAGS+=(--bedrock-only); shift ;;
        --save-transcript) EXTRA_DETECT_FLAGS+=(--save-transcript); shift ;;
        *) fail "Unknown option: $1" ;;
    esac
done

[[ -f "$INPUT_FILE" ]] || fail "File not found: $INPUT_FILE"

# ── Config ────────────────────────────────────────────────────────────────────
section "⚙️  Setup"

if [[ ! -f "$SCRIPT_DIR/config.env" ]]; then
    fail "config.env not found — copy config.env.example and fill in your values."
fi
set -o allexport
# shellcheck disable=SC1091
source "$SCRIPT_DIR/config.env"
set +o allexport
ok "config.env loaded"

if [[ -z "${S3_BUCKET:-}" ]]; then
    fail "S3_BUCKET is not set in config.env — required for AWS Transcribe."
fi
info "S3_BUCKET=${S3_BUCKET}  |  REGION=${AWS_DEFAULT_REGION:-us-east-1}"

# ── Venv ──────────────────────────────────────────────────────────────────────
VENV="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV/bin/python3"
VENV_PIP="$VENV/bin/pip"

if [[ ! -d "$VENV" ]]; then
    info "Creating virtual environment..."
    python3 -m venv "$VENV"
elif ! "$VENV_PYTHON" -c "import sys; sys.exit(0)" 2>/dev/null; then
    warn "Virtual environment is broken — recreating..."
    rm -rf "$VENV"
    python3 -m venv "$VENV"
elif ! head -1 "$VENV/bin/pip" 2>/dev/null | grep -qF "$SCRIPT_DIR"; then
    warn "Virtual environment has stale shebangs — recreating..."
    rm -rf "$VENV"
    python3 -m venv "$VENV"
fi

if ! "$VENV_PYTHON" -c "import boto3, certifi" 2>/dev/null; then
    info "Installing dependencies..."
    "$VENV_PIP" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi
ok "venv ready"

export PYTHONPATH="$SCRIPT_DIR/src:${PYTHONPATH:-}"

# ── Derive paths ──────────────────────────────────────────────────────────────
BOOK_STEM="$(basename "${INPUT_FILE%.*}")"
CHAPTERS_OUT="${BOOK_STEM}_chapters.csv"
[[ -n "$OUTPUT_DIR" ]] && EXTRA_SPLIT_FLAGS+=(--output-dir "$OUTPUT_DIR")

# ── Step 1: Find chapters ─────────────────────────────────────────────────────
if [[ -n "$CHAPTERS_CSV" ]]; then
    section "📖  Chapters"
    [[ -f "$CHAPTERS_CSV" ]] || fail "Chapters file not found: $CHAPTERS_CSV"
    CHAPTERS_OUT="$CHAPTERS_CSV"
    ok "Using existing chapters file: $CHAPTERS_CSV"
else
    section "🎙️  Transcribe & detect chapters"
    info "Input: $INPUT_FILE"
    "$VENV_PYTHON" "$SCRIPT_DIR/find_chapter_timestamps.py" \
        "$INPUT_FILE" \
        --output "$CHAPTERS_OUT" \
        "${EXTRA_DETECT_FLAGS[@]+"${EXTRA_DETECT_FLAGS[@]}"}"
    ok "Chapters written to $CHAPTERS_OUT"
fi

# ── Step 2: Split ─────────────────────────────────────────────────────────────
section "✂️  Splitting into chapters"
info "Chapters: $CHAPTERS_OUT"
"$VENV_PYTHON" "$SCRIPT_DIR/split_audiobook.py" \
    "$INPUT_FILE" \
    --chapters "$CHAPTERS_OUT" \
    "${EXTRA_SPLIT_FLAGS[@]+"${EXTRA_SPLIT_FLAGS[@]}"}"

# ── Done ──────────────────────────────────────────────────────────────────────
section "🎉  Done"
FINAL_DIR="${OUTPUT_DIR:-${BOOK_STEM}_chapters}"
ok "Split files are in: $FINAL_DIR"
