#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# test_ad.sh — one-command ad-cleaner test
#
# Usage:
#   ./test_ad.sh "@aliabdaal"
#   ./test_ad.sh "https://www.youtube.com/watch?v=LLjpnubsOWc"
#   ./test_ad.sh "PLEVkQGIATCXI1F2qs0slVE2MScaj1cSM0"
#   ./test_ad.sh "https://feeds.megaphone.fm/WWO4510910710"
#   ./test_ad.sh "The Tim Ferriss Show"
#   ./test_ad.sh "@aliabdaal" --skip-transcribe    # reuse cached transcript
#   ./test_ad.sh "@aliabdaal" --max-iter 1         # only one verify pass
#   ./test_ad.sh "@aliabdaal" --no-snap            # disable silence snapping
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# --- Help ---
if [[ "${1:-}" == "--help" ]] || [[ "${1:-}" == "-h" ]]; then
    cat << 'HELPEOF'
PodcastDrive — test_ad.sh

Ad-cleaner end-to-end test harness. Downloads one episode, runs the full
ad-removal pipeline, and saves a cleaned MP3 locally for manual listening.

Usage:
  ./test_ad.sh <source> [OPTIONS]

Source (required — one of):
  @ChannelHandle    YouTube channel handle (fetches latest upload)
  https://...       YouTube video URL, playlist URL, or RSS feed URL
  PLxxxxxxxxxx      YouTube playlist ID (fetches latest item)
  "Podcast Name"   Searches iTunes for the podcast, fetches latest episode

Options:
  --help, -h         Show this help message and exit
  --skip-transcribe  Reuse cached transcript (skips AWS Transcribe call)
  --no-snap          Disable silence snapping (cut at exact detected boundaries)
  --max-iter N       Max verification/retry passes (default: 3)
  --out-dir DIR      Output directory for cleaned files (default: ./test_output)

Environment:
  Reads config.env for AWS credentials and ad-removal settings.
  Key variables: BEDROCK_MODEL_ID, AD_SNAP_TO_SILENCE, MAX_AD_SEGMENT_SECS, etc.

Examples:
  ./test_ad.sh "@aliabdaal"
  ./test_ad.sh "https://www.youtube.com/watch?v=LLjpnubsOWc"
  ./test_ad.sh "The Tim Ferriss Show" --skip-transcribe
  ./test_ad.sh "@hubaborern" --no-snap --max-iter 1
HELPEOF
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 1. Load config.env (AWS creds, S3_BUCKET, etc.) ──────────────────────────
if [[ ! -f "$SCRIPT_DIR/config.env" ]]; then
  echo "ERROR: config.env not found — copy config.env.example and fill in your values"
  exit 1
fi
set -o allexport
# shellcheck disable=SC1091
source "$SCRIPT_DIR/config.env"
set +o allexport

# ── 2. Bootstrap venv (recreate if missing, broken, or built under a different path) ──
VENV="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV/bin/python3"
VENV_PIP="$VENV/bin/pip"

if [[ ! -d "$VENV" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv "$VENV"
elif ! "$VENV_PYTHON" -c "import sys; sys.exit(0)" 2>/dev/null; then
  echo "Warning: virtual environment is broken — recreating..."
  rm -rf "$VENV"
  python3 -m venv "$VENV"
elif ! head -1 "$VENV/bin/pip" 2>/dev/null | grep -qF "$SCRIPT_DIR"; then
  echo "Warning: virtual environment has stale shebangs (built under a different path) — recreating..."
  rm -rf "$VENV"
  python3 -m venv "$VENV"
fi

if ! "$VENV_PYTHON" -c "import yt_dlp, boto3, tenacity, certifi" 2>/dev/null; then
  echo "Installing dependencies..."
  "$VENV_PIP" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

# ── 3. Run the test harness ───────────────────────────────────────────────────
export PYTHONPATH="$SCRIPT_DIR/src:${PYTHONPATH:-}"
exec "$VENV_PYTHON" "$SCRIPT_DIR/ad_cleaner_harness.py" "$@"
