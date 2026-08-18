#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# eval/run_e2e_tests.sh — On-demand E2E regression tests for ad removal.
#
# Two tiers:
#   Tier 1 (default): Uses cached transcripts. Only calls Bedrock + ffmpeg.
#                     Fast (~30s/fixture), low cost (~$0.02/fixture).
#   Tier 2 (--full):  Full pipeline including AWS Transcribe.
#                     Slow (~10min/fixture), higher cost (~$0.50/fixture).
#
# Usage:
#   ./eval/run_e2e_tests.sh                   # Tier 1: fast, cached transcript
#   ./eval/run_e2e_tests.sh --full            # Tier 2: full pipeline
#   ./eval/run_e2e_tests.sh --update-gt       # Re-detect + update ground_truth.json
#   ./eval/run_e2e_tests.sh --f1-threshold 0.80  # Override F1 threshold
#
# Exit codes:
#   0 — all fixtures passed F1/recall thresholds and property checks
#   1 — one or more fixtures failed
#   2 — setup error (missing fixtures, missing credentials, etc.)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✅  $*${RESET}"; }
fail() { echo -e "  ${RED}❌  $*${RESET}"; }
warn() { echo -e "  ${YELLOW}⚠️   $*${RESET}"; }
info() { echo -e "  ${CYAN}ℹ️   $*${RESET}"; }

# ── Parse flags ──────────────────────────────────────────────────────────────
FULL=false
UPDATE_GT=false
F1_THRESHOLD="0.75"
RECALL_THRESHOLD="0.70"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --full)             FULL=true ;;
        --update-gt)        UPDATE_GT=true ;;
        --f1-threshold)     F1_THRESHOLD="$2"; shift ;;
        --recall-threshold) RECALL_THRESHOLD="$2"; shift ;;
        --help|-h)
            sed -n '/^# ─/,/^# ─/p' "$0" | grep "^#" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown flag: $1"; exit 2 ;;
    esac
    shift
done

# ── Load config ───────────────────────────────────────────────────────────────
if [[ ! -f "$PROJECT_DIR/config.env" ]]; then
    fail "config.env not found in $PROJECT_DIR — copy config.env.example and fill in values"
    exit 2
fi
set -o allexport
source "$PROJECT_DIR/config.env"
set +o allexport

# ── Activate venv ─────────────────────────────────────────────────────────────
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"
if [[ ! -x "$VENV_PYTHON" ]]; then
    fail "Virtual environment not found at $PROJECT_DIR/.venv — run ./run.sh once to create it"
    exit 2
fi
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

# ── Pre-flight: check fixtures exist ──────────────────────────────────────────
echo ""
echo "PodcastDrive — E2E Regression Tests"
echo "$(printf '─%.0s' {1..52})"
echo ""

EPISODES_DIR="$SCRIPT_DIR/episodes"
if [[ -z "$(ls "$EPISODES_DIR"/*.mp3 2>/dev/null)" ]]; then
    fail "No MP3 fixtures in $EPISODES_DIR"
    info "Add MP3 files to eval/episodes/ to run E2E tests."
    exit 2
fi
FIXTURE_COUNT=$(ls "$EPISODES_DIR"/*.mp3 | wc -l | tr -d ' ')
info "Found $FIXTURE_COUNT fixture(s) in eval/episodes/"

# ── Pre-flight: check ground truth exists ─────────────────────────────────────
GROUND_TRUTH="$SCRIPT_DIR/ground_truth.json"
if [[ ! -f "$GROUND_TRUTH" ]]; then
    fail "ground_truth.json not found — see eval/ground_truth.json template"
    exit 2
fi
if grep -q "NEEDS_ANNOTATION" "$GROUND_TRUTH" 2>/dev/null; then
    warn "ground_truth.json has unannotated fixtures."
    warn "Run: python eval/run_eval.py --update-ground-truth --skip-transcribe"
    warn "Then review eval/ground_truth.json and commit it before using --ci mode."
fi

# ── Pre-flight: check results dir ─────────────────────────────────────────────
mkdir -p "$SCRIPT_DIR/results"

# ── Build eval command ────────────────────────────────────────────────────────
EVAL_CMD=("$VENV_PYTHON" "$SCRIPT_DIR/run_eval.py"
    --score
    --splice
    --check-properties
    --ci
    --f1-threshold "$F1_THRESHOLD"
    --recall-threshold "$RECALL_THRESHOLD"
)

if [[ "$FULL" == true ]]; then
    info "Tier 2: full pipeline (Transcribe + Bedrock + ffmpeg)"
    warn "This will call AWS Transcribe (~\$0.50/fixture) and take ~10 min"
else
    info "Tier 1: fast mode (cached transcripts + Bedrock + ffmpeg)"
    EVAL_CMD+=(--skip-transcribe)
fi

if [[ "$UPDATE_GT" == true ]]; then
    EVAL_CMD+=(--update-ground-truth)
    warn "--update-gt: current detections will overwrite ground_truth.json segments"
fi

echo ""

# ── Run ───────────────────────────────────────────────────────────────────────
set +e
"${EVAL_CMD[@]}"
EXIT_CODE=$?
set -e

echo ""
if [[ $EXIT_CODE -eq 0 ]]; then
    ok "All E2E tests passed"
else
    fail "E2E tests FAILED (exit code $EXIT_CODE)"
    info "Results saved to eval/results/"
fi

exit $EXIT_CODE
