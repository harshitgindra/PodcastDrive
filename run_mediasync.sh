#!/bin/bash
# Run MediaSync — download YouTube media to S3, driven by Notion.
# Usage:
#   ./run_mediasync.sh              # process all pending entries
#   ./run_mediasync.sh --dry-run    # preview without processing
#   ./run_mediasync.sh -v           # verbose output

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="${SCRIPT_DIR}/.venv"

# Load environment
ENV_FILE="${SCRIPT_DIR}/mediasync.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Error: $ENV_FILE not found. Copy mediasync.env.example and fill in values." >&2
    exit 1
fi
set -a
source "$ENV_FILE"
set +a

# Ensure venv exists
if [ ! -d "$VENV" ]; then
    echo "Error: .venv not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# Add venv bin to PATH (for yt-dlp, ffmpeg wrappers)
export PATH="${VENV}/bin:${PATH}"
export PYTHONPATH="${SCRIPT_DIR}/src"

exec "${VENV}/bin/python" -m mediasync "$@"
