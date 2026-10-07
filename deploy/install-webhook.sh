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

# Do not print or return the bearer token. Access must remain local or tunneled.
echo ""
echo "Webhook service restarted and bound to loopback."
echo "Access it through SSM port forwarding; do not expose TCP/9090 publicly."
echo "Token remains in ${ENV_FILE} with mode 0600."
