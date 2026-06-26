#!/bin/bash
# refresh_cookies.sh — Export fresh YouTube cookies from Firefox and deploy to EC2
# Reads Firefox's cookie DB directly (no network, no hanging).
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

echo "[$(date '+%H:%M:%S')] Refreshing YouTube cookies..."

# Step 1: Export fresh cookies from Firefox (reads SQLite directly — instant)
OUTPUT=$("$VENV_PYTHON" "${SCRIPT_DIR}/src/export_cookies.py" "$COOKIES_FILE" 2>&1)
COOKIE_COUNT=$(grep -c "youtube.com\|google.com" "$COOKIES_FILE" 2>/dev/null || echo 0)

if [ "$COOKIE_COUNT" -eq 0 ]; then
  echo "  ⚠️  No YouTube cookies found in Firefox. Are you logged in?"
  exit 0
fi
echo "  ✅ ${OUTPUT}"

# Step 2: Copy to EC2
if ssh $SSH_OPTS "ec2-user@${HOST}" "true" 2>/dev/null; then
  scp $SSH_OPTS "$COOKIES_FILE" "ec2-user@${HOST}:/home/ec2-user/PodcastDrive/cookies.txt" 2>/dev/null
  echo "  ✅ Deployed to EC2 (${HOST})"
else
  echo "  ⚠️  EC2 unreachable (${HOST}). Cookies saved locally only."
fi

echo "[$(date '+%H:%M:%S')] Done."
