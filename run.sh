#!/bin/bash
# Run the podcast sync.
# Usage:
#   ./run.sh                                    # process all from config
#   ./run.sh --dry-run                          # preview all from config (no writes)
#   ./run.sh <playlist_id_or_url> [...]         # process specific playlists
#   ./run.sh --dry-run <playlist_id_or_url> [...] # preview specific playlists

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()      { echo -e "  ${GREEN}✅  $*${RESET}"; }
fail()    { echo -e "  ${RED}❌  $*${RESET}"; exit 1; }
warn()    { echo -e "  ${YELLOW}⚠️   $*${RESET}"; }
info()    { echo -e "  ${CYAN}ℹ️   $*${RESET}"; }
section() { echo -e "\n${BOLD}$*${RESET}\n$(printf '─%.0s' {1..52})"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Ensure logs directory exists ---
LOG_DIR="${LOG_DIR:-${SCRIPT_DIR}/logs}"
mkdir -p "$LOG_DIR"
export LOG_DIR

# --- Parse --dry-run flag ---
DRY_RUN=false
ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--dry-run" ]]; then
        DRY_RUN=true
    else
        ARGS+=("$arg")
    fi
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

if [ "$DRY_RUN" = true ]; then
    PY_DRY_RUN=True
    echo ">>> DRY-RUN mode: no files will be downloaded, uploaded, or deleted <<<"
    echo ""
else
    PY_DRY_RUN=False
fi

# --- Load config ---
section "1 / 2  Environment (config.env)"
CONFIG_FILE="${SCRIPT_DIR}/config.env"
if [ -f "$CONFIG_FILE" ]; then
    set -a
    source "$CONFIG_FILE"
    set +a
    ok "config.env loaded"
else
    fail "config.env not found — copy config.env.example and fill in your values"
fi

# --- Setup venv if needed ---
section "2 / 2  Python environment"
if [ ! -d "${SCRIPT_DIR}/.venv" ]; then
    info "Creating virtual environment..."
    python3 -m venv "${SCRIPT_DIR}/.venv"
fi

VENV_PIP="${SCRIPT_DIR}/.venv/bin/pip"
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python3"
ok "Virtual environment ready"

# Install dependencies if missing
if ! "${VENV_PYTHON}" -c "import yt_dlp, boto3, yaml, certifi" 2>/dev/null; then
    info "Installing dependencies..."
    if [ -f "${SCRIPT_DIR}/requirements.txt" ]; then
        "${VENV_PIP}" install --quiet -r "${SCRIPT_DIR}/requirements.txt"
    else
        "${VENV_PIP}" install --quiet yt-dlp boto3 pyyaml certifi
    fi
fi
ok "Python dependencies installed"

# --- Defaults ---
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-west-2}"

# Required — must be set in config.env
: "${S3_BUCKET:?S3_BUCKET must be set in config.env}"
: "${CLOUDFRONT_BASE:?CLOUDFRONT_BASE must be set in config.env}"
export S3_BUCKET
export CLOUDFRONT_BASE
export CLOUDFRONT_DISTRIBUTION_ID="${CLOUDFRONT_DISTRIBUTION_ID:-}"
export MAX_DOWNLOADS_PER_RUN="${MAX_DOWNLOADS_PER_RUN:-10}"
export MAX_AGE_DAYS="${MAX_AGE_DAYS:-7}"
export SLEEP_BETWEEN_DOWNLOADS="${SLEEP_BETWEEN_DOWNLOADS:-5}"
export CONFIG_PROVIDER="${CONFIG_PROVIDER:-yaml}"
export PODCASTS_YAML="${PODCASTS_YAML:-${SCRIPT_DIR}/podcasts.yaml}"
export PYTHONPATH="${SCRIPT_DIR}/src"

# --- Preflight checks ---
"${VENV_PYTHON}" -c "
import sys, os
sys.path.insert(0, '$SCRIPT_DIR/src')
from preflight import run_preflight
run_preflight(dry_run=$PY_DRY_RUN)
" || exit 1

if [ $# -gt 0 ]; then
    # --- CLI mode: process specific playlists ---
    for INPUT in "$@"; do
        if [[ "$INPUT" == http* ]]; then
            URL="$INPUT"
        elif [[ "$INPUT" == @* ]]; then
            URL="https://www.youtube.com/${INPUT}/videos"
        elif [[ "$INPUT" == PL* ]] || [[ "$INPUT" == UU* ]] || [[ "$INPUT" == UC* ]]; then
            URL="https://www.youtube.com/playlist?list=$INPUT"
        else
            URL="https://www.youtube.com/playlist?list=$INPUT"
        fi

        echo ""
        echo "=========================================="
        echo "Processing: $URL"
        echo "=========================================="
        "${VENV_PYTHON}" -c "
import json
from logger_config import setup_logging
setup_logging()
from sync import process_playlist
result = process_playlist('$URL', dry_run=$PY_DRY_RUN)
print(json.dumps(result, indent=2))
" || echo "ERROR: Failed processing $URL"
    done
else
    # --- Config mode: process all enabled podcasts from config provider ---
    "${VENV_PYTHON}" -c "
import json, sys, os
from logger_config import setup_logging
setup_logging()

from config_provider import get_config_provider
from sync import process_playlist
from utils import extract_playlist_id

provider = get_config_provider()
podcasts = provider.get_podcasts()

enabled = [p for p in podcasts if p.enabled]
print(f'Found {len(enabled)} enabled podcasts (of {len(podcasts)} total)')
print()

for i, podcast in enumerate(enabled):
    # Build full URL from the configured url field
    url = podcast.url
    if not url.startswith('http'):
        if url.startswith('@'):
            url = f'https://www.youtube.com/{url}/videos'
        else:
            url = f'https://www.youtube.com/playlist?list={url}'
    elif '/@' in url and '/videos' not in url:
        # Channel URL without /videos tab — append it
        url = url.rstrip('/') + '/videos'

    print('=' * 50)
    print(f'[{i+1}/{len(enabled)}] {podcast.name}')
    print(f'URL: {url}')
    print('=' * 50)

    try:
        result = process_playlist(
            url,
            max_downloads=podcast.max_downloads,
            max_age_days=podcast.max_age_days,
            sleep_between=podcast.sleep_between,
            dry_run=$PY_DRY_RUN,
        )
        print(json.dumps(result, indent=2))

        # Build feed URL and update Notion
        playlist_id = result.get('playlist_id', '')
        cloudfront_base = os.environ.get('CLOUDFRONT_BASE', '')
        feed_url = f'{cloudfront_base}/{playlist_id}/feed.xml' if playlist_id and cloudfront_base else ''
        provider.update_last_run(podcast, feed_url=feed_url)
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
    print()
" || echo "ERROR: Failed processing podcasts"
fi
