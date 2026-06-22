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
| **Security Group** | `PodcastDrive-SG` (SSH only) |
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

## Provisioning from Scratch

If you ever need to recreate the instance:
```bash
# 1. Create SSH key (if needed)
aws ec2 create-key-pair --key-name <KEY> --region us-west-2 \
  --query 'KeyMaterial' --output text > ~/.ssh/<KEY>.pem
chmod 400 ~/.ssh/<KEY>.pem

# 2. Provision EC2 + IAM
./deploy/provision.sh --key-name <KEY>

# 3. Deploy code + setup + cron
./deploy/deploy.sh
```

All scripts are idempotent — safe to re-run on failure.

## Files

| File | Purpose |
|------|---------|
| `provision.sh` | Creates IAM role, policy, security group, launches EC2 |
| `deploy.sh` | Syncs code to EC2, runs setup, installs cron |
| `setup-ec2.sh` | Runs ON EC2 — installs Python, ffmpeg, yt-dlp, venv |
| `iam-policy.json` | IAM permissions (S3, CloudFront, Bedrock, Transcribe) |
| `trust-policy.json` | Allows EC2 to assume the IAM role |
| `crontab.txt` | Cron schedule (edit this to change run times) |
| `.instance-info` | Auto-generated, git-ignored — stores instance ID/IP for deploy.sh |

## Costs

- `t4g.medium` (2 vCPU, 4GB RAM): ~$24/month on-demand
- Consider a Reserved Instance or Savings Plan if running long-term
- To stop (pause billing): `aws ec2 stop-instances --instance-ids <INSTANCE_ID> --region us-west-2`
- To restart: `aws ec2 start-instances --instance-ids <INSTANCE_ID> --region us-west-2`
  - ⚠️ Public IP changes on restart unless you attach an Elastic IP
