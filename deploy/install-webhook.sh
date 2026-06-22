#!/bin/bash
# install-webhook.sh — Install and start the webhook server on EC2
# Run this ON the instance (called by deploy.sh automatically)
set -euo pipefail

PROJECT_DIR="/home/ec2-user/PodcastDrive"
ENV_FILE="${PROJECT_DIR}/deploy/.webhook-env"

# --- Generate token if not exists ---
if [[ ! -f "$ENV_FILE" ]]; then
  TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  cat > "$ENV_FILE" << ENVEOF
WEBHOOK_TOKEN=$TOKEN
WEBHOOK_PORT=9090
PROJECT_DIR=$PROJECT_DIR
ENVEOF
  chmod 600 "$ENV_FILE"
  echo "  Generated new webhook token."
else
  echo "  Webhook env already exists, keeping existing token."
fi

# --- Install systemd service ---
sudo cp "${PROJECT_DIR}/deploy/webhook.service" /etc/systemd/system/podcastdrive-webhook.service
sudo systemctl daemon-reload
sudo systemctl enable podcastdrive-webhook
sudo systemctl restart podcastdrive-webhook

echo "  Webhook service started."

# --- Print access info ---
TOKEN=$(grep WEBHOOK_TOKEN "$ENV_FILE" | cut -d= -f2-)
IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "<IP>")
echo ""
echo "==========================================="
echo "🔗 Webhook ready!"
echo "==========================================="
echo ""
echo "  Trigger:  curl http://${IP}:9090/run?token=${TOKEN}"
echo "  Status:   curl http://${IP}:9090/status?token=${TOKEN}"
echo "  Logs:     curl http://${IP}:9090/logs?token=${TOKEN}"
echo "  Health:   curl http://${IP}:9090/health  (no auth needed)"
echo ""
echo "  Token: ${TOKEN}"
echo "  (saved in ${ENV_FILE})"
echo "==========================================="
