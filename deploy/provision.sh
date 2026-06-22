#!/bin/bash
# provision.sh — Create EC2 instance with IAM role for PodcastDrive
# Prerequisites: AWS CLI configured with admin access to the target AWS account
# Usage: ./provision.sh [--key-name YOUR_SSH_KEY]
set -euo pipefail

# --- Configuration ---
REGION="us-west-2"
INSTANCE_TYPE="t4g.medium"
AMI_ID=""  # Will auto-resolve to latest Amazon Linux 2023
ROLE_NAME="PodcastDrive-EC2-Role"
POLICY_NAME="PodcastDrive-Policy"
INSTANCE_PROFILE_NAME="PodcastDrive-EC2-Profile"
SG_NAME="PodcastDrive-SG"
VOLUME_SIZE_GB=30
TAG_NAME="PodcastDrive"

# --- Parse args ---
KEY_NAME=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --key-name) KEY_NAME="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$KEY_NAME" ]]; then
  echo "Usage: ./provision.sh --key-name YOUR_SSH_KEY_NAME"
  echo ""
  echo "List available key pairs: aws ec2 describe-key-pairs --region $REGION --query 'KeyPairs[].KeyName' --output text"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== PodcastDrive EC2 Provisioning ==="
echo "Region:   $REGION"
echo "Instance: $INSTANCE_TYPE"
echo "Key:      $KEY_NAME"
echo ""

# --- Step 1: Resolve latest Amazon Linux 2023 AMI ---
echo "→ Resolving latest Amazon Linux 2023 AMI..."
AMI_ID=$(aws ssm get-parameters \
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --region "$REGION" \
  --query 'Parameters[0].Value' \
  --output text)

if [[ -z "$AMI_ID" || "$AMI_ID" == "None" ]]; then
  echo "  Falling back to x86_64..."
  AMI_ID=$(aws ssm get-parameters \
    --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
    --region "$REGION" \
    --query 'Parameters[0].Value' \
    --output text)
fi
echo "  AMI: $AMI_ID"

# --- Step 2: Create IAM Role ---
echo "→ Creating IAM role: $ROLE_NAME..."
if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null; then
  echo "  Role already exists, skipping creation."
else
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://${SCRIPT_DIR}/trust-policy.json" \
    --description "EC2 role for PodcastDrive pipeline" \
    --output text --query 'Role.Arn'
  echo "  Created."
fi

# --- Step 3: Create and attach IAM policy ---
echo "→ Creating IAM policy: $POLICY_NAME..."
# Read S3_BUCKET from config.env if available
S3_BUCKET=""
if [[ -f "${SCRIPT_DIR}/../config.env" ]]; then
  S3_BUCKET=$(grep -E '^S3_BUCKET=' "${SCRIPT_DIR}/../config.env" | cut -d= -f2- | xargs)
fi
if [[ -z "$S3_BUCKET" ]]; then
  echo "  ⚠️  S3_BUCKET not found in config.env — policy will use placeholder."
  echo "  Edit the policy after creation to set the correct bucket ARN."
  S3_BUCKET="YOUR-BUCKET-NAME-HERE"
fi

# Render policy with actual bucket name
POLICY_DOC=$(sed "s/\${S3_BUCKET}/$S3_BUCKET/g" "${SCRIPT_DIR}/iam-policy.json")

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"

if aws iam get-policy --policy-arn "$POLICY_ARN" &>/dev/null; then
  echo "  Policy already exists. Updating to latest version..."
  # Delete oldest version if at limit (max 5)
  VERSIONS=$(aws iam list-policy-versions --policy-arn "$POLICY_ARN" --query 'Versions[?!IsDefaultVersion].VersionId' --output text)
  for v in $VERSIONS; do
    aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$v" 2>/dev/null || true
  done
  aws iam create-policy-version \
    --policy-arn "$POLICY_ARN" \
    --policy-document "$POLICY_DOC" \
    --set-as-default >/dev/null
else
  aws iam create-policy \
    --policy-name "$POLICY_NAME" \
    --policy-document "$POLICY_DOC" \
    --description "S3 + CloudFront + Bedrock + Transcribe for PodcastDrive" \
    --output text --query 'Policy.Arn'
fi

aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN" 2>/dev/null || true
echo "  Policy attached to role."

# --- Step 4: Create Instance Profile ---
echo "→ Creating instance profile: $INSTANCE_PROFILE_NAME..."
if aws iam get-instance-profile --instance-profile-name "$INSTANCE_PROFILE_NAME" &>/dev/null; then
  echo "  Instance profile already exists."
else
  aws iam create-instance-profile --instance-profile-name "$INSTANCE_PROFILE_NAME" >/dev/null
  aws iam add-role-to-instance-profile \
    --instance-profile-name "$INSTANCE_PROFILE_NAME" \
    --role-name "$ROLE_NAME"
  echo "  Created and role attached."
  echo "  Waiting for IAM propagation..."
  sleep 15
fi

# --- Step 5: Create Security Group (SSH only) ---
echo "→ Creating security group: $SG_NAME..."
VPC_ID=$(aws ec2 describe-vpcs --region "$REGION" \
  --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)

SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters Name=group-name,Values="$SG_NAME" Name=vpc-id,Values="$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")

if [[ "$SG_ID" == "None" || -z "$SG_ID" ]]; then
  SG_ID=$(aws ec2 create-security-group \
    --group-name "$SG_NAME" \
    --description "PodcastDrive EC2 - SSH only" \
    --vpc-id "$VPC_ID" \
    --region "$REGION" \
    --query 'GroupId' --output text)
  # Allow SSH from anywhere (you can restrict to your IP later)
  aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --region "$REGION" \
    --protocol tcp --port 22 --cidr 0.0.0.0/0 >/dev/null
  echo "  Created: $SG_ID (SSH open — restrict later if needed)"
else
  echo "  Already exists: $SG_ID"
fi

# --- Step 6: Launch EC2 Instance (with retry for instance profile propagation) ---
echo "→ Launching EC2 instance..."
# Check if one already exists with our tag
EXISTING_ID=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$TAG_NAME" "Name=instance-state-name,Values=running,stopped" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || echo "None")

if [[ "$EXISTING_ID" != "None" && -n "$EXISTING_ID" ]]; then
  echo "  ⚠️  Instance already exists: $EXISTING_ID"
  INSTANCE_ID="$EXISTING_ID"
else
  MAX_RETRIES=5
  RETRY_DELAY=15
  for attempt in $(seq 1 $MAX_RETRIES); do
    INSTANCE_ID=$(aws ec2 run-instances \
      --region "$REGION" \
      --image-id "$AMI_ID" \
      --instance-type "$INSTANCE_TYPE" \
      --key-name "$KEY_NAME" \
      --security-group-ids "$SG_ID" \
      --iam-instance-profile Name="$INSTANCE_PROFILE_NAME" \
      --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=$VOLUME_SIZE_GB,VolumeType=gp3}" \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$TAG_NAME}]" \
      --associate-public-ip-address \
      --query 'Instances[0].InstanceId' \
      --output text 2>&1) && break

    if echo "$INSTANCE_ID" | grep -q "Invalid IAM Instance Profile"; then
      echo "  ⏳ Instance profile not ready yet (attempt $attempt/$MAX_RETRIES), retrying in ${RETRY_DELAY}s..."
      sleep "$RETRY_DELAY"
    else
      echo "  ❌ Launch failed: $INSTANCE_ID"
      exit 1
    fi
  done

  if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == *"error"* || "$INSTANCE_ID" == *"Invalid"* ]]; then
    echo "  ❌ Failed to launch instance after $MAX_RETRIES attempts."
    exit 1
  fi
  echo "  Launched: $INSTANCE_ID"
fi

# --- Step 7: Wait and get public IP ---
echo "→ Waiting for instance to be running..."
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"

PUBLIC_IP=$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --region "$REGION" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text)

echo ""
echo "==========================================="
echo "✅ EC2 instance ready!"
echo "==========================================="
echo "  Instance ID: $INSTANCE_ID"
echo "  Public IP:   $PUBLIC_IP"
echo "  SSH:         ssh -i ~/.ssh/${KEY_NAME}.pem ec2-user@${PUBLIC_IP}"
echo ""
echo "Next steps:"
echo "  1. Run: ./deploy/deploy.sh --host $PUBLIC_IP --key ~/.ssh/${KEY_NAME}.pem"
echo "  2. SSH in and verify: ./run.sh --dry-run"
echo "==========================================="

# Save instance info for deploy.sh
cat > "${SCRIPT_DIR}/.instance-info" << INST
INSTANCE_ID=$INSTANCE_ID
PUBLIC_IP=$PUBLIC_IP
REGION=$REGION
KEY_NAME=$KEY_NAME
INST
echo "Instance info saved to deploy/.instance-info"
