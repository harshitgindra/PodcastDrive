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

# --- Failure alerting ---
# mediasync.cli sends a Telegram summary at the end of a *completed* pipeline
# run, including one where `stats.failed > 0`.  Everything that exits before
# that point -- a missing env file, a broken venv, a config error, an unhandled
# exception -- was visible only in a log nobody reads.  This covers those paths.
#
# The alert carries the exit code, a fixed reason and the log path only.  Log
# contents can contain tokens, so they are never forwarded.
NOTIFY_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mediasync-run.XXXXXX")"
SUMMARY_SENTINEL="${NOTIFY_DIR}/summary-sent"
# mediasync.cli touches this once a summary is actually delivered; that is how
# the two sides agree on who alerts, without changing any exit code.
export MEDIASYNC_NOTIFY_SENTINEL="$SUMMARY_SENTINEL"
trap 'rm -rf "$NOTIFY_DIR"' EXIT

alert_failure() {
    local status="$1" reason="$2" log_file="${3:-}"

    local enabled
    enabled="$(printf '%s' "${MEDIASYNC_HERALD_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
    if [ "$enabled" != "true" ]; then
        return 0
    fi

    if ! command -v herald >/dev/null 2>&1; then
        echo "herald not on PATH — MediaSync failure alert not sent." >&2
        return 0
    fi

    local message="MediaSync FAILED (exit ${status})
  ${reason}"
    if [ -n "$log_file" ]; then
        message="${message}
  Log: ${log_file}"
    fi

    local cmd=(herald notify --parse-mode plain --strict --message "$message")
    if [ -n "${HERALD_JOB_ID:-}" ]; then
        cmd+=(--job "$HERALD_JOB_ID")
    fi

    if ! "${cmd[@]}" >/dev/null 2>&1; then
        echo "herald notify failed — MediaSync failure alert not delivered." >&2
    fi
    return 0
}

# --- Validate environment file ---
ENV_FILE="${SCRIPT_DIR}/mediasync.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Error: $ENV_FILE not found. Copy mediasync.env.example and fill in values." >&2
    alert_failure 1 "mediasync.env is missing — nothing ran."
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
    alert_failure 1 "No usable .venv — nothing ran."
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
    if [ -e "$SUMMARY_SENTINEL" ]; then
        echo "run summary already delivered; no extra failure alert" >>"$LOG_FILE"
    else
        alert_failure "$STATUS" "Exited before sending a run summary." "$LOG_FILE"
    fi
fi
exit "$STATUS"
