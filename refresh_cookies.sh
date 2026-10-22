#!/bin/bash
# refresh_cookies.sh — Export fresh YouTube cookies from Chrome (local only)
# Uses yt-dlp's built-in Chrome cookie decryption (handles macOS Keychain).
# Falls back to Firefox. The existing cookies.txt is replaced only once a fresh
# export with YouTube/Google cookies succeeds, and it is always mode 600.
# Usage: ./refresh_cookies.sh
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COOKIES_FILE="${SCRIPT_DIR}/cookies.txt"
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python3"
EXPORTER="${SCRIPT_DIR}/src/export_cookies.py"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "ERROR: venv not found at ${VENV_PYTHON}. Run ./run.sh once to bootstrap."
  exit 1
fi

STAGED="$(mktemp "${SCRIPT_DIR}/.cookies-refresh.XXXXXX")"
trap 'rm -f "$STAGED"' EXIT

echo "[$(date +%H:%M:%S)] Refreshing YouTube cookies..."

# yt-dlp writes the browser's whole cookie jar; --filter trims it to the
# YouTube/Google domains before it can replace cookies.txt.
if "$VENV_PYTHON" -m yt_dlp \
  --cookies-from-browser chrome \
  --cookies "$STAGED" \
  --skip-download \
  --no-warnings \
  --no-update \
  --print title \
  "https://www.youtube.com/watch?v=dQw4w9WgXcQ" > /dev/null 2>&1 \
  && OUTPUT=$("$VENV_PYTHON" "$EXPORTER" --filter "$STAGED" 2>&1); then
  echo "  ${OUTPUT}"
else
  echo "  Warning: Chrome cookie export failed. Trying Firefox..."
  if ! OUTPUT=$("$VENV_PYTHON" "$EXPORTER" "$STAGED" 2>&1) || ! grep -q $'\t' "$STAGED"; then
    echo "  ${OUTPUT:-Firefox export produced no cookies}"
    echo "  Warning: No YouTube cookies found. Are you logged into YouTube in Chrome or Firefox? Keeping the existing cookies.txt."
    exit 1
  fi
  echo "  ${OUTPUT}"
fi

chmod 600 "$STAGED"
mv -f "$STAGED" "$COOKIES_FILE"
echo "  OK: cookies.txt updated"

echo "[$(date +%H:%M:%S)] Done."
