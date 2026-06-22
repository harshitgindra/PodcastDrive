#!/bin/bash
# deploy.sh — Deploy PodcastDrive to the EC2 instance
# Usage: ./deploy.sh --host IP --key PATH_TO_PEM
#        ./deploy.sh  (reads from .instance-info)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# --- Parse args ---
HOST=""
KEY=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --host) HOST="$2"; shift 2 ;;
    --key)  KEY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Fall back to .instance-info
if [[ -z "$HOST" && -f "${SCRIPT_DIR}/.instance-info" ]]; then
  source "${SCRIPT_DIR}/.instance-info"
  HOST="${PUBLIC_IP:-}"
fi
if [[ -z "$KEY" && -f "${SCRIPT_DIR}/.instance-info" ]]; then
  source "${SCRIPT_DIR}/.instance-info"
  KEY="${HOME}/.ssh/${KEY_NAME}.pem"
fi

if [[ -z "$HOST" || -z "$KEY" ]]; then
  echo "Usage: ./deploy.sh --host EC2_IP --key ~/.ssh/yourkey.pem"
  exit 1
fi

SSH_OPTS="-i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
SSH="ssh $SSH_OPTS ec2-user@$HOST"
SCP="scp $SSH_OPTS"

echo "=== Deploying PodcastDrive to $HOST ==="

# --- Step 0: Wait for SSH to become available ---
echo "→ Waiting for SSH to be ready..."
MAX_SSH_WAIT=120
INTERVAL=5
ELAPSED=0
while ! $SSH "true" &>/dev/null; do
  if [[ $ELAPSED -ge $MAX_SSH_WAIT ]]; then
    echo "  ❌ SSH not available after ${MAX_SSH_WAIT}s. Instance may still be booting."
    echo "  Retry with: ./deploy.sh --host $HOST --key $KEY"
    exit 1
  fi
  echo "  ⏳ SSH not ready yet (${ELAPSED}s / ${MAX_SSH_WAIT}s)..."
  sleep $INTERVAL
  ELAPSED=$((ELAPSED + INTERVAL))
done
echo "  ✅ SSH connected (took ${ELAPSED}s)"

# --- Step 1: Sync project files ---
echo "→ Syncing project files..."
rsync -az --progress \
  -e "ssh $SSH_OPTS" \
  --exclude '.venv/' \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude 'logs/' \
  --exclude 'reports/' \
  --exclude '.podcastdrive.lock' \
  --exclude 'deploy/.instance-info' \
  "$PROJECT_DIR/" "ec2-user@${HOST}:/home/ec2-user/PodcastDrive/"

# --- Step 2: Copy config.env ---
if [[ -f "${PROJECT_DIR}/config.env" ]]; then
  echo "→ Copying config.env..."
  $SCP "${PROJECT_DIR}/config.env" "ec2-user@${HOST}:/home/ec2-user/PodcastDrive/config.env"
else
  echo "  ⚠️  No config.env found locally. You'll need to create it on the instance."
fi

# --- Step 3: Run setup ---
echo "→ Running setup on instance..."
$SSH "chmod +x /home/ec2-user/PodcastDrive/deploy/setup-ec2.sh && /home/ec2-user/PodcastDrive/deploy/setup-ec2.sh"

# --- Step 4: Install cron ---
echo "→ Installing cron schedule..."
$SSH "crontab /home/ec2-user/PodcastDrive/deploy/crontab.txt"
echo "  Cron installed. Verify with: ssh ec2-user@$HOST 'crontab -l'"

# --- Step 5: Create logs dir ---
$SSH "mkdir -p /home/ec2-user/PodcastDrive/logs"

echo ""
echo "==========================================="
echo "✅ Deployment complete!"
echo "==========================================="
echo "  SSH:     ssh -i $KEY ec2-user@$HOST"
echo "  Test:    ssh -i $KEY ec2-user@$HOST 'cd PodcastDrive && ./run.sh --dry-run'"
echo "  Logs:    ssh -i $KEY ec2-user@$HOST 'tail -f PodcastDrive/logs/cron.log'"
echo "  Cron:    ssh -i $KEY ec2-user@$HOST 'crontab -l'"
echo ""
echo "Management:"
echo "  Pause:   ssh -i $KEY ec2-user@$HOST 'crontab -r'"
echo "  Resume:  ssh -i $KEY ec2-user@$HOST 'crontab PodcastDrive/deploy/crontab.txt'"
echo "  Redeploy: ./deploy.sh --host $HOST --key $KEY"
echo "==========================================="
