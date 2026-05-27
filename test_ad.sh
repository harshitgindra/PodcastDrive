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

# ── 2. Bootstrap venv (first run creates it, subsequent runs skip) ────────────
VENV="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV/bin/python3"
VENV_PIP="$VENV/bin/pip"

if [[ ! -d "$VENV" ]]; then
  echo "Creating virtual environment..."
  /opt/homebrew/bin/python3 -m venv "$VENV" 2>/dev/null \
    || /usr/local/bin/python3 -m venv "$VENV" 2>/dev/null \
    || python3 -m venv "$VENV"
fi

if ! "$VENV_PYTHON" -c "import yt_dlp, boto3, tenacity, certifi" 2>/dev/null; then
  echo "Installing dependencies..."
  "$VENV_PIP" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

# ── 3. Run the test harness ───────────────────────────────────────────────────
export PYTHONPATH="$SCRIPT_DIR/src:${PYTHONPATH:-}"
exec "$VENV_PYTHON" "$SCRIPT_DIR/test_ad_cleaner.py" "$@"
