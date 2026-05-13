#!/usr/bin/env bash
#
# Deploy a personal DCV instance
#
# Usage:
#   ./deploy-dcv.sh <username>
#   ./deploy-dcv.sh alice
#   ./deploy-dcv.sh alice r7i.4xlarge
#
# Prerequisites:
#   - Main CDK (../cdk) must be deployed first (VPC, FSx, SSM params)
#   - AWS credentials configured for ap-northeast-2
#
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

USERNAME="${1:-}"
INSTANCE_TYPE="${2:-r7i.2xlarge}"

[[ -n "${USERNAME}" ]] || error "Usage: $0 <username> [instance-type]"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

# Check prerequisites
command -v cdk >/dev/null 2>&1 || error "CDK CLI not installed"
aws ssm get-parameter --name /eda/network/VpcId --query 'Parameter.Value' --output text >/dev/null 2>&1 \
  || error "SSM parameters not found. Deploy main CDK (../cdk) first"

# Activate venv
if [[ ! -d ".venv" ]]; then
  info "Creating Python venv..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Deploy
info "Deploying DCV instance for: ${USERNAME} (${INSTANCE_TYPE})"

cdk deploy "EdaDcv-${USERNAME}" \
  -c "eda:dcv_username=${USERNAME}" \
  -c "eda:dcv_instance_type=${INSTANCE_TYPE}" \
  --require-approval never \
  --outputs-file "outputs-${USERNAME}.json"

# Show connection info
echo ""
PRIVATE_IP=$(jq -r ".\"EdaDcv-${USERNAME}\".PrivateIp" "outputs-${USERNAME}.json")
KEY_PAIR=$(aws ssm get-parameter --name /eda/network/KeyPairName --query 'Parameter.Value' --output text)

echo "══════════════════════════════════════════"
echo "  DCV Instance: ${USERNAME}"
echo "  Private IP:   ${PRIVATE_IP}"
echo "  Instance:     ${INSTANCE_TYPE}"
echo ""
echo "  DCV (browser via VPN):"
echo "    https://${PRIVATE_IP}:8443"
echo ""
echo "  SSH:"
echo "    ssh -i ~/.ssh/${KEY_PAIR}.pem ec2-user@${PRIVATE_IP}"
echo ""
echo "  DCV login:"
echo "    Username: ${USERNAME}"
echo "    Password: changeme123 (change on first login)"
echo "══════════════════════════════════════════"
