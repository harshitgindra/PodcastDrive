#!/usr/bin/env bash
# Run MediaSync — download YouTube media to OneDrive/S3, driven by Notion.
#
# This is the canonical MediaSync entry point. scripts/run_mediasync.sh is a
# thin forwarder kept for the Herald `mediasync` service definition.
#
# Usage:
#   ./run_mediasync.sh              # process all pending entries
#   ./run_mediasync.sh --dry-run    # preview without processing
#   ./run_mediasync.sh -v           # verbose output

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${SCRIPT_DIR}/.venv"

# --- Validate environment file ---
ENV_FILE="${SCRIPT_DIR}/mediasync.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Error: $ENV_FILE not found. Copy mediasync.env.example and fill in values." >&2
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# --- Validate venv ---
if [ ! -x "${VENV}/bin/python" ]; then
    echo "Error: .venv not found or not executable at ${VENV}." >&2
    echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# yt-dlp and ffmpeg wrappers live in .venv/bin
export PATH="${VENV}/bin:${PATH}"

# mediasync lives under src/ and is not pip-installed, so nothing but the test
# conftest puts it on the path. Without this, `python -m mediasync` fails with
# "No module named mediasync" regardless of the working directory.
# Appended (not assigned) so an inherited PYTHONPATH is preserved.
export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"

# --- Always persist a log ---
# Herald invokes this with `reply: exit`, which routes stdout/stderr to
# DEVNULL. Without an on-disk log a failure is undiagnosable after the fact,
# which is how a real ENOENT failure went unexplained. Tee unconditionally.
LOG_DIR="${LOG_DIR:-${SCRIPT_DIR}/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/mediasync-$(date -u +%Y%m%dT%H%M%SZ).log"

{
    echo "=== MediaSync run $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    echo "args: $*"
    echo "storage: ${MEDIASYNC_STORAGE:-<unset>}"
} >>"$LOG_FILE"

# errexit must be off here: otherwise a failing pipeline aborts the script
# before the exit line and the failure message below are ever written, which
# defeats the whole point of keeping a log.
set +e
"${VENV}/bin/python" -m mediasync "$@" 2>&1 | tee -a "$LOG_FILE"
STATUS=${PIPESTATUS[0]}
set -e

echo "=== exit ${STATUS} at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >>"$LOG_FILE"
if [ "$STATUS" -ne 0 ]; then
    echo "MediaSync failed (exit ${STATUS}). Log: ${LOG_FILE}" >&2
fi
exit "$STATUS"
