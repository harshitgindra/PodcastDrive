#!/bin/bash
# health.sh — Run health report and self-healing
# Usage:
#   ./health.sh                  # health report (last 7 days)
#   ./health.sh --days 14        # health report (last 14 days)
#   ./health.sh --heal           # run self-healing (dry-run)
#   ./health.sh --heal --apply   # run self-healing (for real)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python3"
export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"

# Load config for S3_BUCKET and AWS_DEFAULT_REGION
if [ -f "${SCRIPT_DIR}/config.env" ]; then
  set -a
  source "${SCRIPT_DIR}/config.env"
  set +a
fi
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-west-2}"

# --- Parse args ---
DAYS=7
HEAL=false
APPLY=false
OUTPUT="md"

for arg in "$@"; do
  case $arg in
    --days) shift; DAYS="${1:-7}"; shift ;;
    --heal) HEAL=true ;;
    --apply) APPLY=true ;;
    --json) OUTPUT="json" ;;
    --help|-h)
      echo "Usage: ./health.sh [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --days N     Analyze last N days (default: 7)"
      echo "  --json       Output report as JSON instead of markdown"
      echo "  --heal       Run self-healing (dry-run by default)"
      echo "  --apply      Apply self-healing changes (use with --heal)"
      echo ""
      echo "Examples:"
      echo "  ./health.sh                  # 7-day health report"
      echo "  ./health.sh --days 14        # 14-day health report"
      echo "  ./health.sh --heal           # preview self-healing actions"
      echo "  ./health.sh --heal --apply   # execute self-healing"
      exit 0
      ;;
  esac
done

if [ "$HEAL" = true ]; then
  if [ "$APPLY" = true ]; then
    echo "=== Running Self-Healing (LIVE) ==="
    "${VENV_PYTHON}" -m self_heal
  else
    echo "=== Running Self-Healing (DRY-RUN) ==="
    "${VENV_PYTHON}" -m self_heal --dry-run
  fi
else
  echo "=== PodcastDrive Health Report (last ${DAYS} days) ==="
  echo ""
  "${VENV_PYTHON}" -m health_report --days "$DAYS" --output "$OUTPUT"
fi
