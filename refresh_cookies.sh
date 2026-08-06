#!/bin/bash
# refresh_cookies.sh — Export fresh YouTube cookies from Chrome and deploy to EC2
# Uses yt-dlp'"'"'s built-in Chrome cookie decryption (handles macOS Keychain).
# Usage: ./refresh_cookies.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COOKIES_FILE="${SCRIPT_DIR}/cookies.txt"
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python3"

# Load instance info for EC2 target
if [ -f "${SCRIPT_DIR}/deploy/.instance-info" ]; then
  source "${SCRIPT_DIR}/deploy/.instance-info"
else
  echo "ERROR: deploy/.instance-info not found. Run provision.sh first."
  exit 1
fi

SSH_KEY="${HOME}/.ssh/${KEY_NAME}.pem"
SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=no -o LogLevel=ERROR -o ConnectTimeout=10"
HOST="${PUBLIC_IP}"

echo "[$(date +%H:%M:%S)] Refreshing YouTube cookies..."

# Step 1: Export cookies from Chrome using yt-dlp (decrypts Keychain-encrypted values).
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
  exit 0
fi
echo "  OK: Exported $COOKIE_COUNT YouTube/Google cookies from Chrome"

# Step 2: Copy to EC2
if ssh $SSH_OPTS "ec2-user@${HOST}" "true" 2>/dev/null; then
  scp $SSH_OPTS "$COOKIES_FILE" "ec2-user@${HOST}:/home/ec2-user/PodcastDrive/cookies.txt" 2>/dev/null
  echo "  OK: Deployed to EC2 (${HOST})"
else
  echo "  Warning: EC2 unreachable (${HOST}). Cookies saved locally only."
fi

echo "[$(date +%H:%M:%S)] Done."