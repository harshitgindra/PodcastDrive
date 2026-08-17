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

# mediasync lives under src/ and is not pip-installed, so nothing but the test
# conftest puts it on the path. Without this, `python -m mediasync` fails with
# "No module named mediasync" regardless of the working directory.
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$PROJECT_DIR/.venv/bin/python" -m mediasync "$@"
