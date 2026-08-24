#!/bin/bash
# refresh_cookies.sh — Export fresh YouTube cookies from Chrome (local only)
# Uses yt-dlp'\''s built-in Chrome cookie decryption (handles macOS Keychain).
# Usage: ./refresh_cookies.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COOKIES_FILE="${SCRIPT_DIR}/cookies.txt"
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python3"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "ERROR: venv not found at ${VENV_PYTHON}. Run ./run.sh once to bootstrap."
  exit 1
fi

echo "[$(date +%H:%M:%S)] Refreshing YouTube cookies..."

# Export cookies from Chrome using yt-dlp (decrypts Keychain-encrypted values).
# Falls back to Firefox direct SQLite read if Chrome fails.
"$VENV_PYTHON" -m yt_dlp \
  --cookies-from-browser chrome \
  --cookies "$COOKIES_FILE" \
  --skip-download \
  --no-warnings \
  --no-update \
  --print title \
  "https://www.youtube.com/watch?v=dQw4w9WgXcQ" > /dev/null 2>&1
EXPORT_RC=$?

if [ $EXPORT_RC -ne 0 ]; then
  echo "  Warning: Chrome cookie export failed (rc=$EXPORT_RC). Trying Firefox..."
  OUTPUT=$("$VENV_PYTHON" "${SCRIPT_DIR}/src/export_cookies.py" "$COOKIES_FILE" 2>&1)
  echo "  ${OUTPUT}"
fi

COOKIE_COUNT=$(grep -c "youtube.com\|google.com" "$COOKIES_FILE" 2>/dev/null || echo 0)

if [ "$COOKIE_COUNT" -eq 0 ]; then
  echo "  Warning: No YouTube cookies found. Are you logged into YouTube in Chrome?"
  exit 1
fi
echo "  OK: Exported $COOKIE_COUNT YouTube/Google cookies"

echo "[$(date +%H:%M:%S)] Done."