#!/bin/bash
# Run the podcast sync.
# Usage:
#   ./run.sh                                    # process all from config
#   ./run.sh --dry-run                          # preview all from config (no writes)
#   ./run.sh <playlist_id_or_url> [...]         # process specific playlists
#   ./run.sh --dry-run <playlist_id_or_url> [...] # preview specific playlists
#   ./run.sh --reset                            # delete all S3 data for enabled podcasts (prompts)
#   ./run.sh --reset --force                    # same, skip confirmation prompt
#   ./run.sh --clear-cache <slug|all>           # delete ad-segments cache (forces re-detection)
#   ./run.sh --reprocess [playlist|@handle|all] # delete episodes + cache, then re-sync with ad removal

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

# --- Lock file to prevent concurrent runs (#18) ---
LOCK_FILE="${SCRIPT_DIR}/.podcastdrive.lock"

cleanup() {
    rm -f "$LOCK_FILE"
    # Release distributed lock (S3) — safe even if not acquired
    if [ -n "${VENV_PYTHON:-}" ] && [ -x "${VENV_PYTHON:-}" ]; then
      PYTHONPATH="${SCRIPT_DIR}/src" "${VENV_PYTHON}" -c "
from distributed_lock import S3Lock
S3Lock().release()
" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        fail "Another instance is running (PID $LOCK_PID). Remove $LOCK_FILE if stale."
    else
        warn "Stale lock file found (PID $LOCK_PID no longer running) — removing."
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"

# --- Help ---
show_help() {
    cat << 'HELPEOF'
PodcastDrive — run.sh

Usage:
  ./run.sh [OPTIONS] [TARGETS...]

Options:
  --help            Show this help message and exit
  --dry-run         Preview mode — no downloads, uploads, or deletions
  --reset           Delete ALL S3 data for enabled podcasts (prompts for confirmation)
  --reset --force   Same as --reset but skip the confirmation prompt
  --clear-cache <slug|all>
                    Delete ad-segments cache for a podcast (forces re-detection on next run)
  --reprocess <target|all>
                    Delete episodes + ad cache, then re-sync with current ad-removal settings.
                    Useful after tuning ad-detection parameters or upgrading the Bedrock model.

Targets (optional — if omitted, processes all enabled podcasts from config):
  PLxxxxxxxxxx      YouTube playlist ID
  @ChannelHandle    YouTube channel handle
  https://...       YouTube URL (playlist, channel, or video)

Reprocess targets:
  PLxxxxxxxxxx      YouTube playlist ID
  @ChannelHandle    YouTube channel handle
  podcast-slug      Slug of an RSS podcast (as shown in S3)
  all               Reprocess all enabled podcasts

Environment:
  All behaviour is configured via config.env (see config.env.example).
  Key variables: S3_BUCKET, CLOUDFRONT_BASE, REMOVE_ADS, MAX_AGE_DAYS, etc.
  See README.md for the full environment variable reference.

Examples:
  ./run.sh                                  # sync all enabled podcasts
  ./run.sh --dry-run                        # preview what would happen
  ./run.sh @aliabdaal                       # sync a specific YouTube channel
  ./run.sh --reprocess all                  # re-download + re-clean everything
  ./run.sh --reprocess @aliabdaal           # re-clean one channel
  ./run.sh --clear-cache all                # force ad re-detection on next run
  ./run.sh --reset --force                  # wipe all S3 data (no prompt)
HELPEOF
    exit 0
}

# --- Parse flags: --dry-run, --reset, --force ---
DRY_RUN=false
DO_RESET=false
DO_CLEAR_CACHE=false
DO_REPROCESS=false
FORCE=false
ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--help" ]] || [[ "$arg" == "-h" ]]; then
        show_help
    fi
done
for arg in "$@"; do
    if [[ "$arg" == "--dry-run" ]]; then
        DRY_RUN=true
    elif [[ "$arg" == "--reset" ]]; then
        DO_RESET=true
    elif [[ "$arg" == "--force" ]]; then
        FORCE=true
    elif [[ "$arg" == "--clear-cache" ]]; then
        DO_CLEAR_CACHE=true
    elif [[ "$arg" == "--reprocess" ]]; then
        DO_REPROCESS=true
    else
        ARGS+=("$arg")
    fi
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

if [ "$DRY_RUN" = true ]; then
    echo ">>> DRY-RUN mode: no files will be downloaded, uploaded, or deleted <<<"
    echo ""
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

# --- Setup venv (recreate if missing, broken, or built under a different path) ---
section "2 / 2  Python environment"
if [ ! -d "${SCRIPT_DIR}/.venv" ]; then
    info "Creating virtual environment..."
    python3 -m venv "${SCRIPT_DIR}/.venv"
elif ! "${SCRIPT_DIR}/.venv/bin/python3" -c "import sys; sys.exit(0)" 2>/dev/null; then
    warn "Virtual environment is broken (interpreter not executable) — recreating..."
    rm -rf "${SCRIPT_DIR}/.venv"
    python3 -m venv "${SCRIPT_DIR}/.venv"
elif ! head -1 "${SCRIPT_DIR}/.venv/bin/pip" 2>/dev/null | grep -qF "${SCRIPT_DIR}"; then
    warn "Virtual environment has stale shebangs (built under a different path) — recreating..."
    rm -rf "${SCRIPT_DIR}/.venv"
    python3 -m venv "${SCRIPT_DIR}/.venv"
fi

VENV_PIP="${SCRIPT_DIR}/.venv/bin/pip"
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python3"
export PATH="${HOME}/.deno/bin:${SCRIPT_DIR}/.venv/bin:$PATH"
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

# --- Runner identification ---
# Auto-detect: hostname + trigger source (cron/webhook/manual)
# TRIGGER can be set by cron wrapper or webhook; defaults to "manual"
_HOSTNAME=$(hostname -s 2>/dev/null || echo "unknown")
export RUNNER="${RUNNER:-${_HOSTNAME}/${TRIGGER:-manual}}"

# --- Distributed lock (prevents concurrent runs across machines) ---
if [ "$DRY_RUN" = false ]; then
  LOCK_OUTPUT=$("${VENV_PYTHON}" -c "
from distributed_lock import S3Lock, LockAcquireError
import sys
try:
    lock = S3Lock()
    lock.acquire()
    print(\"acquired\")
except LockAcquireError as e:
    print(f\"LOCKED:{e}\")
    sys.exit(99)
except Exception as e:
    print(f\"ERROR:{type(e).__name__}: {e}\")
    sys.exit(1)
" 2>&1) || true
  LOCK_EXIT=$?
  if [ "$LOCK_EXIT" -eq 99 ]; then
    warn "Another machine is running PodcastDrive: ${LOCK_OUTPUT#LOCKED:}"
    warn "Skipping this run."
    exit 0
  elif [ "$LOCK_EXIT" -ne 0 ]; then
    warn "Distributed lock failed: ${LOCK_OUTPUT#ERROR:}"
    warn "Proceeding without distributed lock."
  fi
fi

# --- Record run start (S3 history) ---
if [ "$DRY_RUN" = false ]; then
  "${VENV_PYTHON}" -c "
from run_history import record_run_start, save_run_history
import json, os
record = record_run_start()
save_run_history(record)
# Save record to temp file for end-of-run update
with open(os.path.join(os.environ.get(\"LOG_DIR\", \"logs\"), \".run_record.json\"), \"w\") as f:
    json.dump(record, f)
" 2>/dev/null || true
fi

# --- Reset mode: wipe all S3 data for enabled podcasts then exit ---
if [ "$DO_RESET" = true ]; then
    FORCE_FLAG=""
    if [ "$FORCE" = true ]; then
        FORCE_FLAG="--force"
    fi
    PODCAST_DRY_RUN="false" "${VENV_PYTHON}" -c "
import sys, os
from logger_config import setup_logging
setup_logging()
from reset import run_reset
force = '--force' in sys.argv[1:]
sys.exit(run_reset(force=force))
" ${FORCE_FLAG}
    exit $?
fi

# --- Cache-bust mode: delete ad-segments cache for a podcast slug ---
if [ "${DO_CLEAR_CACHE:-false}" = true ]; then
    TARGET_SLUG="${1:-}"
    if [ -z "$TARGET_SLUG" ]; then
        fail "--clear-cache requires a podcast slug or 'all' as argument"
    fi
    if [ "$TARGET_SLUG" = "all" ]; then
        PREFIX="transcribe-cache/"
        echo "Clearing ALL ad-segments caches under s3://${S3_BUCKET}/${PREFIX}"
        aws s3 rm "s3://${S3_BUCKET}/${PREFIX}" --recursive --exclude "*" --include "*_ads.json"
    else
        PREFIX="transcribe-cache/"
        echo "Clearing ad-segments cache for: ${TARGET_SLUG}"
        # List and delete only _ads.json files — transcript (.json) and text (.txt) are kept
        aws s3 ls "s3://${S3_BUCKET}/${PREFIX}" | awk '{print $4}' |              grep "_ads\.json$" | while read -r key; do
                aws s3 rm "s3://${S3_BUCKET}/${PREFIX}${key}"
                echo "  Deleted: ${PREFIX}${key}"
            done || true
    fi
    exit 0
fi

# --- Reprocess mode: delete episodes + cache, then fall through to normal sync ---
if [ "${DO_REPROCESS:-false}" = true ]; then
    # Determine which slug(s) to reprocess
    REPROCESS_TARGETS=()
    if [ $# -gt 0 ] && [ "$1" != "all" ]; then
        REPROCESS_TARGETS=("$@")
    fi

    if [ ${#REPROCESS_TARGETS[@]} -eq 0 ] && [ "${1:-}" != "all" ] && [ $# -eq 0 ]; then
        fail "--reprocess requires a playlist ID, @handle, or 'all' as argument.
  Examples:
    ./run.sh --reprocess PLxxxxxxxxx
    ./run.sh --reprocess @channelHandle
    ./run.sh --reprocess all"
    fi

    section "Reprocess: clearing S3 episodes + ad-segment caches"

    # Resolve slugs
    SLUGS=()
    if [ "${1:-}" = "all" ]; then
        info "Reprocessing ALL enabled podcasts"
        # Collect all slugs from config
        SLUG_LIST=$("${VENV_PYTHON}" -c "
import sys, os, re
from config_provider import get_config_provider, get_podcast_config_provider
from utils import extract_playlist_id

slugs = []
try:
    yt = get_config_provider()
    for p in yt.get_podcasts():
        if not p.enabled: continue
        try:
            slugs.append(extract_playlist_id(p.url))
        except Exception:
            pass
except Exception:
    pass
try:
    rss = get_podcast_config_provider()
    for p in rss.get_podcasts():
        if not p.enabled: continue
        slug = p.name.lower()
        slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')[:60] or 'podcast'
        slugs.append(slug)
except Exception:
    pass
for s in slugs:
    print(s)
" 2>/dev/null)
        while IFS= read -r slug; do
            [ -n "$slug" ] && SLUGS+=("$slug")
        done <<< "$SLUG_LIST"
    else
        for INPUT in "${REPROCESS_TARGETS[@]}"; do
            if [[ "$INPUT" == @* ]]; then
                SLUGS+=("$INPUT")
            elif [[ "$INPUT" == PL* ]] || [[ "$INPUT" == UU* ]] || [[ "$INPUT" == UC* ]]; then
                SLUGS+=("$INPUT")
            elif [[ "$INPUT" == http* ]]; then
                # Extract playlist ID from URL
                SLUG=$("${VENV_PYTHON}" -c "
from utils import extract_playlist_id
print(extract_playlist_id('$INPUT'))
" 2>/dev/null || echo "$INPUT")
                SLUGS+=("$SLUG")
            else
                # Raw RSS podcast slug (e.g. a podcast name or slug like "the-best-one-yet").
                # Mark it so we clear positional args after cleanup — the sync phase must NOT
                # try to treat this as a YouTube playlist ID.
                SLUGS+=("$INPUT")
                HAS_RSS_SLUG=true
            fi
        done
    fi

    if [ ${#SLUGS[@]} -eq 0 ]; then
        fail "Could not resolve any podcast slugs to reprocess"
    fi

    echo "  Will reprocess ${#SLUGS[@]} podcast(s):"
    for slug in "${SLUGS[@]}"; do
        echo "    • $slug"
    done
    echo ""

    for slug in "${SLUGS[@]}"; do
        # Delete episode MP3s
        EP_PREFIX="${slug}/episodes/"
        EP_COUNT=$(aws s3 ls "s3://${S3_BUCKET}/${EP_PREFIX}" 2>/dev/null | wc -l | tr -d " " || true)
        EP_COUNT="${EP_COUNT:-0}"
        if [ "$EP_COUNT" -gt 0 ] 2>/dev/null; then
            info "Deleting ${EP_COUNT} episode(s) from s3://${S3_BUCKET}/${EP_PREFIX}"
            aws s3 rm "s3://${S3_BUCKET}/${EP_PREFIX}" --recursive --quiet
        else
            info "No episodes in S3 for ${slug} — nothing to delete"
        fi

        # Delete feed.xml and manifest
        aws s3 rm "s3://${S3_BUCKET}/${slug}/feed.xml" --quiet 2>/dev/null || true
        aws s3 rm "s3://${S3_BUCKET}/${slug}/manifest.json" --quiet 2>/dev/null || true

        # Clear ad-segments cache (keep transcripts — they save Transcribe cost)
        CACHE_PREFIX="transcribe-cache/"
        aws s3 ls "s3://${S3_BUCKET}/${CACHE_PREFIX}" 2>/dev/null | awk '{print $4}' |              grep "_ads\.json$" | while read -r key; do
                aws s3 rm "s3://${S3_BUCKET}/${CACHE_PREFIX}${key}" --quiet
            done || true
    done

    ok "Cleared episodes + ad caches. Now re-syncing with ad removal..."
    echo ""

    # Clear positional args and fall through to normal sync
    # (process all from config, which will now re-download the deleted episodes)
    #
    # We clear args in two cases:
    #   1. The target was "all"
    #   2. The target was a raw RSS podcast slug (e.g. "The Best One Yet" / "the-best-one-yet")
    #      — these are NOT valid YouTube playlist IDs and must not be passed to the sync phase,
    #      which would try to call extract_playlist_id() on them and raise:
    #      "Playlist ID contains unsafe characters".
    if [ "${1:-}" = "all" ] || [ "${HAS_RSS_SLUG:-false}" = "true" ]; then
        set --
    fi
fi

# --- Track overall run status ---
RUN_STATUS="success"

# --- Preflight checks ---
PODCAST_DRY_RUN="$DRY_RUN" "${VENV_PYTHON}" -c "
import sys, os
from preflight import run_preflight
dry_run = os.environ.get('PODCAST_DRY_RUN', 'false') == 'true'
run_preflight(dry_run=dry_run)
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
        PODCAST_URL="$URL" PODCAST_DRY_RUN="$DRY_RUN" "${VENV_PYTHON}" -c "
import json, os, sys
from logger_config import setup_logging
setup_logging()
from sync import process_playlist
from config_provider import get_config_provider
from utils import extract_playlist_id

provider = get_config_provider()
url = os.environ['PODCAST_URL']
dry_run = os.environ.get('PODCAST_DRY_RUN', 'false') == 'true'

# Resolve the playlist_id from the URL so we can match it against Notion entries
try:
    playlist_id_for_lookup = extract_playlist_id(url)
except Exception:
    playlist_id_for_lookup = None

# Try to find a matching Notion entry (silently skip if not found or provider unsupported)
notion_podcast = None
if not dry_run and playlist_id_for_lookup and hasattr(provider, 'find_page_by_url'):
    try:
        notion_podcast = provider.find_page_by_url(playlist_id_for_lookup)
    except Exception:
        notion_podcast = None

try:
    if notion_podcast and not dry_run:
        provider.update_status(notion_podcast, 'Running')
        provider.update_last_run(notion_podcast)

    result = process_playlist(url, dry_run=dry_run)
    print(json.dumps(result, indent=2))

    if notion_podcast and not dry_run:
        cloudfront_base = os.environ.get('CLOUDFRONT_BASE', '')
        pid = result.get('playlist_id', '')
        feed_url = f'{cloudfront_base}/{pid}/feed.xml' if pid and cloudfront_base else ''
        provider.update_status(notion_podcast, 'Done')
        provider.update_last_run(notion_podcast, feed_url=feed_url)

except Exception as e:
    if notion_podcast and not dry_run:
        try:
            provider.update_status(notion_podcast, 'Failed')
            provider.update_last_run(notion_podcast)
        except Exception:
            pass
    print(f'ERROR: {e}', file=sys.stderr)
" || { echo "ERROR: Failed processing $URL"; RUN_STATUS="partial_failure"; }
    done
else
    # --- Config mode: process all enabled podcasts from config provider ---
    PODCAST_DRY_RUN="$DRY_RUN" "${VENV_PYTHON}" -c "
import json, sys, os
from logger_config import setup_logging
setup_logging()

from config_provider import get_config_provider
from sync import process_playlist
from utils import extract_playlist_id

dry_run = os.environ.get('PODCAST_DRY_RUN', 'false') == 'true'

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
        if not dry_run:
            provider.update_status(podcast, 'Running')
            provider.update_last_run(podcast)
        result = process_playlist(
            url,
            max_downloads=podcast.max_downloads,
            max_age_days=podcast.max_age_days,
            sleep_between=podcast.sleep_between,
            dry_run=dry_run,
        )
        print(json.dumps(result, indent=2))

        # Build feed URL and update Notion
        playlist_id = result.get('playlist_id', '')
        cloudfront_base = os.environ.get('CLOUDFRONT_BASE', '')
        feed_url = f'{cloudfront_base}/{playlist_id}/feed.xml' if playlist_id and cloudfront_base else ''
        if not dry_run:
            if result.get('bot_detected'):
                provider.update_status(podcast, 'Error: Bot Detection')
            else:
                provider.update_status(podcast, 'Done')
            provider.update_last_run(podcast, feed_url=feed_url)
    except Exception as e:
        if not dry_run:
            provider.update_status(podcast, 'Failed')
            provider.update_last_run(podcast)
        print(f'ERROR: {e}', file=sys.stderr)
    print()
" || { echo "ERROR: Failed processing YouTube podcasts"; RUN_STATUS="partial_failure"; }

    # --- RSS Podcast feeds (Source=Podcast) ---
    PODCAST_DRY_RUN="$DRY_RUN" "${VENV_PYTHON}" -c "
import json, sys, os
from logger_config import setup_logging
setup_logging()

from config_provider import get_podcast_config_provider
from podcast_sync import process_podcast_feed

dry_run = os.environ.get('PODCAST_DRY_RUN', 'false') == 'true'

provider = get_podcast_config_provider()
podcasts = provider.get_podcasts()

enabled = [p for p in podcasts if p.enabled]
print(f'Found {len(enabled)} enabled RSS podcast feeds (of {len(podcasts)} total)')
print()

for i, podcast in enumerate(enabled):
    print('=' * 50)
    print(f'[RSS {i+1}/{len(enabled)}] {podcast.name}')
    print(f'URL: {podcast.url}')
    print('=' * 50)

    try:
        if not dry_run:
            provider.update_status(podcast, 'Running')
            provider.update_last_run(podcast)
        result = process_podcast_feed(podcast, provider=provider, dry_run=dry_run)
        print(json.dumps(result, indent=2))

        # Build feed URL and update Notion
        slug = result.get('slug', '')
        cloudfront_base = os.environ.get('CLOUDFRONT_BASE', '')
        feed_url = f'{cloudfront_base}/{slug}/feed.xml' if slug and cloudfront_base else ''
        if not dry_run:
            provider.update_status(podcast, 'Done')
            provider.update_last_run(podcast, feed_url=feed_url)
    except Exception as e:
        if not dry_run:
            provider.update_status(podcast, 'Failed')
            provider.update_last_run(podcast)
        print(f'ERROR: {e}', file=sys.stderr)
    print()
" || { echo "ERROR: Failed processing RSS podcast feeds"; RUN_STATUS="partial_failure"; }
fi

# --- Run complete summary ---
ELAPSED=$(( SECONDS ))
ELAPSED_MIN=$(( ELAPSED / 60 ))
ELAPSED_SEC=$(( ELAPSED % 60 ))
echo ""
section "Run Complete"
ok "Finished in ${ELAPSED_MIN}m ${ELAPSED_SEC}s"
echo "  Runner: ${RUNNER}"
echo "  Log dir: ${LOG_DIR}"
echo "  Timestamp: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# --- Record run end + upload logs to S3 ---
if [ "$DRY_RUN" = false ]; then
  PODCAST_RUN_STATUS="$RUN_STATUS" "${VENV_PYTHON}" -c "
import json, os
from run_history import record_run_end, save_run_history
from log_uploader import upload_run_log
record_file = os.path.join(os.environ.get(\"LOG_DIR\", \"logs\"), \".run_record.json\")
if os.path.exists(record_file):
    with open(record_file) as f:
        record = json.load(f)
    status = os.environ.get(\"PODCAST_RUN_STATUS\", \"success\")
    record = record_run_end(record, status=status)
    save_run_history(record)
    os.remove(record_file)
upload_run_log()
" 2>/dev/null || true
fi

exit 0
