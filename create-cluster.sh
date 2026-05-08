#!/usr/bin/env bash
#
# EDA ParallelCluster creation script
#
# Run after CDK deployment is complete:
#   ./create-cluster.sh
#
# Options:
#   CLUSTER_NAME  Change cluster name via environment variable (default: eda-cluster)
#   DRY_RUN=1     Only generate config file, skip cluster creation
#   ENABLE_SSM    Set to 1 to enable SSM Session Manager access (default: 0)
#
set -euo pipefail

REGION="ap-northeast-2"
CLUSTER_NAME="${CLUSTER_NAME:-eda-cluster}"
STACK_PREFIX="${STACK_PREFIX:-Eda}"
BASE_STACK="${STACK_PREFIX}Base"
STORAGE_STACK="${STACK_PREFIX}Storage"
CDK_DIR="$(cd "$(dirname "$0")/cdk" && pwd)"
OUTPUTS_FILE="${CDK_DIR}/outputs.json"
TEMPLATE_FILE="${CDK_DIR}/pcluster-config-template.yaml"
CONFIG_FILE="${CDK_DIR}/pcluster-config.yaml"

# ── Colors ───────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── 1. Pre-validation ────────────────────────────────────────
info "Starting pre-validation"

command -v aws >/dev/null 2>&1     || error "aws CLI is not installed"
command -v pcluster >/dev/null 2>&1 || error "pcluster CLI is not installed. pip install 'aws-parallelcluster>=3.14,<3.15'"
command -v jq >/dev/null 2>&1      || error "jq is not installed"

# Verify AWS credentials
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
  || error "Unable to verify AWS credentials"
info "AWS Account: ${ACCOUNT_ID}, Region: ${REGION}"

# Verify CDK outputs file
[[ -f "${OUTPUTS_FILE}" ]] || error "CDK outputs file not found: ${OUTPUTS_FILE}\n  Run 'cd cdk && cdk deploy --all --outputs-file outputs.json' first"
[[ -f "${TEMPLATE_FILE}" ]] || error "ParallelCluster template not found: ${TEMPLATE_FILE}"

# ── 2. Extract values from CDK outputs ───────────────────────
info "Extracting resource IDs from CDK outputs"

SUBNET_ID=$(jq -r --arg s "${BASE_STACK}" '.[$s].PrimarySubnetId' "${OUTPUTS_FILE}")
SG_CLUSTER=$(jq -r --arg s "${BASE_STACK}" '.[$s].SgClusterNodesId' "${OUTPUTS_FILE}")
KEY_PAIR_NAME=$(jq -r --arg s "${BASE_STACK}" '.[$s].KeyPairName' "${OUTPUTS_FILE}")
KEY_PAIR_ID=$(jq -r --arg s "${BASE_STACK}" '.[$s].KeyPairId' "${OUTPUTS_FILE}")
VOL_TOOLS=$(jq -r --arg s "${STORAGE_STACK}" '.[$s].VolToolsId' "${OUTPUTS_FILE}")
VOL_WORK=$(jq -r --arg s "${STORAGE_STACK}" '.[$s].VolWorkId' "${OUTPUTS_FILE}")
VOL_SCRATCH=$(jq -r --arg s "${STORAGE_STACK}" '.[$s].VolScratchId' "${OUTPUTS_FILE}")

# Validate values
for var_name in SUBNET_ID SG_CLUSTER KEY_PAIR_NAME KEY_PAIR_ID VOL_TOOLS VOL_WORK VOL_SCRATCH; do
  val="${!var_name}"
  [[ "${val}" != "null" && -n "${val}" ]] || error "${var_name} value not found in outputs.json"
done

echo "  Subnet:     ${SUBNET_ID}"
echo "  SG:         ${SG_CLUSTER}"
echo "  KeyPair:    ${KEY_PAIR_NAME}"
echo "  Vol Tools:  ${VOL_TOOLS}"
echo "  Vol Work:   ${VOL_WORK}"
echo "  Vol Scratch: ${VOL_SCRATCH}"

# ── 3. Generate ParallelCluster config file ──────────────────
info "Generating ParallelCluster config file: ${CONFIG_FILE}"

sed \
  -e "s|\${BASE.PrimarySubnetId}|${SUBNET_ID}|g" \
  -e "s|\${BASE.SgClusterNodesId}|${SG_CLUSTER}|g" \
  -e "s|\${BASE.KeyPairName}|${KEY_PAIR_NAME}|g" \
  -e "s|\${STORAGE.VolToolsId}|${VOL_TOOLS}|g" \
  -e "s|\${STORAGE.VolWorkId}|${VOL_WORK}|g" \
  -e "s|\${STORAGE.VolScratchId}|${VOL_SCRATCH}|g" \
  "${TEMPLATE_FILE}" > "${CONFIG_FILE}"

# SSM enablement handling
if [[ "${ENABLE_SSM:-0}" == "1" ]]; then
  info "Enabling SSM Session Manager"
  sed -i.bak \
    -e '/^  #SSM_BEGIN/d' \
    -e '/^  #SSM_END/d' \
    -e 's/^  #\(Iam:\)/  \1/' \
    -e 's/^  #\(  AdditionalIamPolicies:\)/  \1/' \
    -e 's/^  #\(    - Policy:.*\)/  \1/' \
    "${CONFIG_FILE}"
  rm -f "${CONFIG_FILE}.bak"
else
  info "SSM Session Manager disabled (default)"
  sed -i.bak \
    -e '/^  #SSM_BEGIN/d' \
    -e '/^  #SSM_END/d' \
    -e '/^  #Iam:/d' \
    -e '/^  #  AdditionalIamPolicies:/d' \
    -e '/^  #    - Policy:.*SSM/d' \
    "${CONFIG_FILE}"
  rm -f "${CONFIG_FILE}.bak"
fi

# Check for unsubstituted placeholders (excluding comments)
if grep -v '^#' "${CONFIG_FILE}" | grep -q '${'; then
  warn "Unsubstituted placeholders found:"
  grep -v '^#' "${CONFIG_FILE}" | grep '${' || true
  error "Config file contains unsubstituted values"
fi

info "Config file generation complete"
echo ""
echo "────────────────────────────────────────"
cat "${CONFIG_FILE}"
echo "────────────────────────────────────────"
echo ""

# ── 4. Download SSH Private Key (무조건 재다운로드 — KeyPair 재생성 시 stale 방지) ─
SSH_KEY_FILE="${HOME}/.ssh/${KEY_PAIR_NAME}.pem"
info "Downloading SSH private key from SSM Parameter Store..."
mkdir -p "${HOME}/.ssh"
aws ssm get-parameter \
  --name "/ec2/keypair/${KEY_PAIR_ID}" \
  --with-decryption \
  --query 'Parameter.Value' \
  --output text \
  --region "${REGION}" > "${SSH_KEY_FILE}" \
  || error "Failed to download SSH private key"
chmod 400 "${SSH_KEY_FILE}"
info "SSH key saved: ${SSH_KEY_FILE}"

# ── 5. DRY_RUN check ─────────────────────────────────────────
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  info "DRY_RUN mode: Only generated config file (${CONFIG_FILE})"
  exit 0
fi

# ── 6. Check for existing cluster ─────────────────────────────
EXISTING=$(pcluster describe-cluster --cluster-name "${CLUSTER_NAME}" --region "${REGION}" 2>/dev/null \
  | jq -r '.clusterStatus // empty' 2>/dev/null || true)

if [[ -n "${EXISTING}" ]]; then
  warn "Cluster '${CLUSTER_NAME}' already exists (status: ${EXISTING})"
  if [[ "${EXISTING}" == "CREATE_COMPLETE" || "${EXISTING}" == "UPDATE_COMPLETE" ]]; then
    read -rp "Update the existing cluster? (y/N): " REPLY
    if [[ "${REPLY}" =~ ^[Yy]$ ]]; then
      info "Starting cluster update: ${CLUSTER_NAME}"
      pcluster update-cluster \
        --cluster-name "${CLUSTER_NAME}" \
        --cluster-configuration "${CONFIG_FILE}" \
        --region "${REGION}"
      info "Update has started. Check status:"
      echo "  pcluster describe-cluster --cluster-name ${CLUSTER_NAME} --region ${REGION}"
      exit 0
    fi
  fi
  error "Cluster already exists. Delete it and try again, or change CLUSTER_NAME"
fi

# ── 7. Create cluster ─────────────────────────────────────────
info "Starting ParallelCluster creation: ${CLUSTER_NAME}"
echo ""

pcluster create-cluster \
  --cluster-name "${CLUSTER_NAME}" \
  --cluster-configuration "${CONFIG_FILE}" \
  --region "${REGION}"

echo ""
info "Cluster creation has started (takes 10-15 minutes)"
echo ""

# ── 8. Status polling ────────────────────────────────────────
info "Monitoring creation status..."
echo ""

while true; do
  STATUS=$(pcluster describe-cluster --cluster-name "${CLUSTER_NAME}" --region "${REGION}" \
    | jq -r '.clusterStatus' 2>/dev/null || echo "UNKNOWN")

  TIMESTAMP=$(date '+%H:%M:%S')

  case "${STATUS}" in
    CREATE_COMPLETE)
      echo ""
      info "Cluster creation complete!"
      echo ""

      # Print cluster info
      CLUSTER_INFO=$(pcluster describe-cluster --cluster-name "${CLUSTER_NAME}" --region "${REGION}")
      HEAD_IP=$(echo "${CLUSTER_INFO}" | jq -r '.headNode.privateIpAddress // "N/A"')
      LOGIN_ADDR=$(echo "${CLUSTER_INFO}" | jq -r '.loginNodes[0].address // "N/A"')

      echo "══════════════════════════════════════════"
      echo "  Cluster:     ${CLUSTER_NAME}"
      echo "  Status:      CREATE_COMPLETE"
      echo "  Head Node:   ${HEAD_IP} (private)"
      echo "  Login Node:  ${LOGIN_ADDR}"
      echo ""
      echo "  Login Node access (via VPN):"
      echo "    ssh -i ${SSH_KEY_FILE} ec2-user@${LOGIN_ADDR}"
      echo ""
      echo "  Head Node access (SSM):"
      echo "    pcluster ssh --cluster-name ${CLUSTER_NAME} --region ${REGION}"
      echo ""
      echo "  Delete:"
      echo "    pcluster delete-cluster --cluster-name ${CLUSTER_NAME} --region ${REGION}"
      echo "══════════════════════════════════════════"
      exit 0
      ;;
    CREATE_FAILED|ROLLBACK_COMPLETE|ROLLBACK_IN_PROGRESS|DELETE_*)
      echo ""
      error "Cluster creation failed (status: ${STATUS})\n  pcluster describe-cluster --cluster-name ${CLUSTER_NAME} --region ${REGION}"
      ;;
    *)
      echo -ne "\r  [${TIMESTAMP}] Status: ${STATUS}  "
      sleep 30
      ;;
  esac
done
