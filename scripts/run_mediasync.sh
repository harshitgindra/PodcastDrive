#!/usr/bin/env bash
# Run the MediaSync pipeline (OneDrive backend)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load env
set -a
source "$PROJECT_DIR/mediasync.env"
set +a

# yt-dlp lives in .venv/bin
export PATH="$PROJECT_DIR/.venv/bin:$PATH"

exec "$PROJECT_DIR/.venv/bin/python" -m mediasync "$@"
