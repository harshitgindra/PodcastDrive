#!/bin/bash
# setup-ec2.sh — Bootstrap PodcastDrive dependencies on Amazon Linux 2023
# Run this ON the EC2 instance after deploying the project files.
set -euo pipefail

echo "=== PodcastDrive EC2 Setup ==="

# --- System packages ---
echo "→ Installing system packages..."
sudo dnf update -y -q
sudo dnf install -y -q python3.11 python3.11-pip git cronie tar gzip

# --- Install ffmpeg (static binary — not in AL2023 repos) ---
echo "→ Installing ffmpeg..."
if command -v ffmpeg &>/dev/null; then
  echo "  ffmpeg already installed: $(ffmpeg -version 2>&1 | head -1)"
else
  ARCH=$(uname -m)
  if [[ "$ARCH" == "aarch64" ]]; then
    FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"
  else
    FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
  fi
  echo "  Downloading static ffmpeg for $ARCH..."
  curl -sL "$FFMPEG_URL" -o /tmp/ffmpeg.tar.xz
  tar -xf /tmp/ffmpeg.tar.xz -C /tmp/
  sudo cp /tmp/ffmpeg-*-static/ffmpeg /usr/local/bin/
  sudo cp /tmp/ffmpeg-*-static/ffprobe /usr/local/bin/
  sudo chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe
  rm -rf /tmp/ffmpeg.tar.xz /tmp/ffmpeg-*-static
  echo "  Installed: $(ffmpeg -version 2>&1 | head -1)"
fi

# Start cron daemon
sudo systemctl enable crond
sudo systemctl start crond

# --- Set timezone to America/Los_Angeles (PDT/PST) ---
echo "→ Setting timezone to America/Los_Angeles..."
sudo timedatectl set-timezone America/Los_Angeles

# --- Project directory ---
PROJECT_DIR="/home/ec2-user/PodcastDrive"
cd "$PROJECT_DIR"

# --- Python venv ---
echo "→ Creating Python venv..."
python3.11 -m venv .venv
source .venv/bin/activate

echo "→ Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# --- yt-dlp (latest) ---
echo "→ Installing yt-dlp..."
pip install --upgrade yt-dlp -q

# --- Verify ---
echo ""
echo "→ Verifying installations..."
echo "  Python:  $(python3 --version)"
echo "  pip:     $(pip --version | awk '{print $2}')"
echo "  ffmpeg:  $(ffmpeg -version 2>&1 | head -1)"
echo "  ffprobe: $(ffprobe -version 2>&1 | head -1)"
echo "  yt-dlp:  $(yt-dlp --version)"
echo "  cron:    $(systemctl is-active crond)"
echo "  TZ:      $(timedatectl show -p Timezone --value)"

echo ""
echo "✅ Setup complete!"
echo "Next: copy config.env, then run: ./run.sh --dry-run"
