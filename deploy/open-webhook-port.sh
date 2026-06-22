#!/bin/bash
# open-webhook-port.sh — Add port 9090 to the PodcastDrive security group
# Run this LOCALLY (needs AWS CLI with appropriate permissions)
set -euo pipefail

REGION="us-west-2"
SG_NAME="PodcastDrive-SG"

# Find security group
VPC_ID=$(aws ec2 describe-vpcs --region "$REGION" \
  --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)

SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters Name=group-name,Values="$SG_NAME" Name=vpc-id,Values="$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text)

if [[ -z "$SG_ID" || "$SG_ID" == "None" ]]; then
  echo "❌ Security group '$SG_NAME' not found."
  exit 1
fi

# Check if rule already exists
EXISTING=$(aws ec2 describe-security-groups --region "$REGION" \
  --group-ids "$SG_ID" \
  --query "SecurityGroups[0].IpPermissions[?FromPort==\`9090\`]" --output text)

if [[ -n "$EXISTING" ]]; then
  echo "✅ Port 9090 already open in $SG_NAME ($SG_ID)"
else
  aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --region "$REGION" \
    --protocol tcp --port 9090 --cidr 0.0.0.0/0
  echo "✅ Opened port 9090 in $SG_NAME ($SG_ID)"
fi
