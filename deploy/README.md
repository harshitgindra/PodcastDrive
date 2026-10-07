# PodcastDrive — EC2 Deployment

## Infrastructure

| Resource | Value |
|----------|-------|
| **Instance ID** | See `deploy/.instance-info` |
| **Public IP** | See `deploy/.instance-info` |
| **Region** | `us-west-2` |
| **Instance Type** | `t4g.medium` (ARM/Graviton) |
| **OS** | Amazon Linux 2023 |
| **SSH Key** | Name configured during provisioning |
| **IAM Role** | `PodcastDrive-EC2-Role` |
| **Security Group** | `PodcastDrive-SG` (SSH; webhook port 9090 must not be public) |
| **Timezone** | `America/Los_Angeles` (PDT/PST) |

> After provisioning, instance details are saved to `deploy/.instance-info` (git-ignored).
> Use variables below as `<IP>` and `<KEY>` — substitute from `.instance-info` or your own values.

## SSH Access

```bash
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP>
```

## Schedule (Cron)

Runs 4 times daily at:

| PDT | What |
|-----|------|
| 6:30 AM | `./run.sh` |
| 8:30 AM | `./run.sh` |
| 12:30 PM | `./run.sh` |
| 3:30 PM | `./run.sh` |

The cron job is installed by `deploy.sh`. To verify:
```bash
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP> 'crontab -l'
```

### Changing the Schedule

1. Edit `deploy/crontab.txt` locally
2. Re-deploy (applies new crontab automatically):
   ```bash
   ./deploy/deploy.sh
   ```

Or apply directly on the instance:
```bash
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP> 'crontab PodcastDrive/deploy/crontab.txt'
```

### Pause / Resume

```bash
# Pause (removes all cron entries)
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP> 'crontab -r'

# Resume (re-installs cron entries)
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP> 'crontab PodcastDrive/deploy/crontab.txt'
```

## HTTP Webhook (private access only)

The webhook binds to loopback and does not provide TLS. **Do not expose TCP/9090
in the EC2 security group or access it over plain HTTP.** The old
`open-webhook-port.sh` now refuses to add public ingress. If port 9090 was
previously opened, remove the existing security-group ingress rule separately.

Use AWS Systems Manager port forwarding from an authorized machine instead:

```bash
aws ssm start-session \
  --target <INSTANCE_ID> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["9090"],"localPortNumber":["9090"]}' \
  --region us-west-2
```

Then, in another terminal, use `http://127.0.0.1:9090`. Keep the bearer token
in the `Authorization` header, never in a URL. The token is stored in
`deploy/.webhook-env` on the instance with mode `0600`; the installer does not
print it. Rotate it if it may have been exposed.

```bash
curl -X POST -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:9090/run
curl -X POST -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:9090/status
curl -X POST -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:9090/logs
curl http://127.0.0.1:9090/health
```

`/run`, `/status`, and `/logs` require authentication. `/health` returns only
liveness. Triggering a run requires POST; GET is not accepted for actions.

Environment variables:
- `WEBHOOK_TOKEN` — Required. Shared secret for Bearer auth.
- `WEBHOOK_PORT` — Listen port (default: `9090`).
- `WEBHOOK_BIND` — Must be a loopback address; non-loopback binding is rejected.
- `PROJECT_DIR` — Path to the PodcastDrive repo on disk.

## Manual Operations

```bash
# Force a run right now
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP> 'cd PodcastDrive && ./run.sh'

# Dry run (no writes)
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP> 'cd PodcastDrive && ./run.sh --dry-run'

# View latest logs
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP> 'tail -50 PodcastDrive/logs/cron.log'

# Follow logs in real-time
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP> 'tail -f PodcastDrive/logs/cron.log'
```

## Health Report & Self-Healing

Run locally or on any machine with `config.env`:

```bash
# Health report (last 7 days — markdown)
./health.sh

# Health report (last 14 days — JSON)
./health.sh --days 14 --json

# Self-healing dry-run (preview what would be fixed)
./health.sh --heal

# Self-healing apply (execute fixes)
./health.sh --heal --apply
```

Or via SSH on EC2:
```bash
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP> 'cd PodcastDrive && ./health.sh'
ssh -i ~/.ssh/<KEY>.pem ec2-user@<IP> 'cd PodcastDrive && ./health.sh --heal'
```

### What the health report shows:
- Run summary (total, success rate, avg duration)
- Runs by machine and trigger type
- Error categories (splice, transcribe, download, etc.)
- Top repeated errors and warnings
- Chronic patterns and stale locks
- Actionable recommendations

### What self-healing does (safe operations only):
- **Retry queue**: Tracks failed episodes for automatic retry on next run
- **Cache clear**: Deletes stale ad-segment cache for episodes with 2+ splice failures
- **Manifest backfill**: Fills missing `upload_date` from yt-dlp metadata

## Observability

### Log Storage
Logs are uploaded to S3 after every run:
```
s3://<bucket>/_meta/logs/<date>/<runner>_<timestamp>.jsonl
```

Each log line is JSON (machine-parseable). Run history is tracked at:
```
s3://<bucket>/_meta/runs.jsonl
```

### Distributed Lock
A lock at `s3://<bucket>/_meta/run.lock` prevents concurrent runs across machines.
TTL: 1 hour. Automatically released on completion (success or failure).

### Runner Identification
Every run is tagged with `<hostname>/<trigger>`:
- `ip-172-31-5-42/cron` — EC2 scheduled run
- `Harshits-MacBook/manual` — local manual run
- `ip-172-31-5-42/webhook` — privately triggered webhook run

Visible in: log lines, Notion "Runner" column, S3 run history.

## Deploying Code Updates

After making local changes, push them to the instance:
```bash
./deploy/deploy.sh
```

This will:
- rsync changed files (skips .venv, .git, logs)
- Re-copy config.env
- Re-run setup (idempotent — only installs missing deps)
- Re-install crontab
- Restart webhook server

## Provisioning from Scratch

If you ever need to recreate the instance:
```bash
# 1. Create SSH key (if needed)
aws ec2 create-key-pair --key-name <KEY> --region us-west-2 \
  --query 'KeyMaterial' --output text > ~/.ssh/<KEY>.pem
chmod 400 ~/.ssh/<KEY>.pem

# 2. Provision EC2 + IAM
./deploy/provision.sh --key-name <KEY>

# 3. Deploy code + setup + cron + loopback-only webhook
./deploy/deploy.sh
```

All scripts are idempotent — safe to re-run on failure.

## Files

| File | Purpose |
|------|---------|
| `provision.sh` | Creates IAM role, policy, security group, launches EC2 |
| `deploy.sh` | Syncs code to EC2, runs setup, installs cron + webhook |
| `setup-ec2.sh` | Runs ON EC2 — installs Python, ffmpeg, yt-dlp, venv |
| `iam-policy.json` | IAM permissions (S3, CloudFront, Bedrock, Transcribe) |
| `trust-policy.json` | Allows EC2 to assume the IAM role |
| `crontab.txt` | Cron schedule (edit this to change run times) |
| `webhook.py` | HTTP server for remote triggering (/run, /status, /logs) |
| `webhook.service` | systemd unit for webhook auto-restart |
| `install-webhook.sh` | Generates auth token, installs systemd service |
| `open-webhook-port.sh` | Refuses unsafe public ingress for port 9090 |
| `.instance-info` | Auto-generated, git-ignored — instance ID/IP/key |
| `.webhook-env` | Auto-generated, git-ignored — webhook auth token |

## Costs

- `t4g.medium` (2 vCPU, 4GB RAM): ~$24/month on-demand
- Consider a Reserved Instance or Savings Plan if running long-term
- To stop (pause billing): `aws ec2 stop-instances --instance-ids <INSTANCE_ID> --region us-west-2`
- To restart: `aws ec2 start-instances --instance-ids <INSTANCE_ID> --region us-west-2`
  - ⚠️ Public IP changes on restart unless you attach an Elastic IP
