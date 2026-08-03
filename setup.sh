#!/usr/bin/env bash
#
# EDA on AWS — Local Setup Script
#
# Usage:
#   git clone https://github.com/<org>/eda-aws.git
#   cd eda-aws
#   ./setup.sh                                   # uses config/default.env
#   CONFIG=config/prod.env ./setup.sh            # custom config
#   VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh   # env override
#
# What this script does:
#   1. Pre-validation (AWS credentials, required tools)
#   2. Install CDK Python dependencies
#   3. Install pcluster CLI
#   4. CDK bootstrap + deploy (VPC, FSx)
#   5. Generate ParallelCluster configuration file
#   6. (Optional) Create ParallelCluster
#
# Config resolution order (first non-empty wins):
#   1) Environment variables (export FOO=bar or FOO=bar ./setup.sh)
#   2) Config file (CONFIG=... or config/default.env)
#   3) In-script fallback default (${VAR:-default})
#
# Environment variables (see config/default.env for full descriptions):
#   VPC_ID                    (required) Existing VPC ID (e.g. vpc-xxxxxxxx)
#   SUBNET_ID                 (required) Existing private subnet ID (e.g. subnet-xxxxxxxx)
#                             If not set, falls back to cdk.json (eda:vpc_id / eda:subnet_id)
#   CLUSTER_NAME              Cluster name (default: eda-cluster)
#   SKIP_CDK                  Set to 1 to skip CDK deployment (if already deployed)
#   SKIP_CLUSTER              Set to 1 to skip cluster creation (only generate config file)
#   SKIP_CONNECTIVITY_CHECK   Set to 1 to bypass subnet 0.0.0.0/0 route + VPC endpoint check
#   ENABLE_SSM                Set to 1 to enable SSM Session Manager access (default: 0)
#   STACK_PREFIX              CDK stack prefix (default: Eda) — {prefix}Base, {prefix}Storage, {prefix}LicenseServer
#
#   ── Storage options ──────────────────────────────────────────────────
#   ENABLE_OPENZFS            1 to create FSx OpenZFS (default: 1)
#   OPENZFS_SIZE_GIB          GiB, 64 ~ 524288 (512 TiB) (default: 10240)
#   OPENZFS_THROUGHPUT        MBps, one of 160|320|640|1280|2560|3840|5120|7680|10240 (default: 2560)
#   ENABLE_ONTAP              1 to create FSx NetApp ONTAP (default: 0)
#   ONTAP_SIZE_GIB            GiB, 1024 ~ 1048576 (1 PiB) (default: 10240)
#   ONTAP_TPUT_PER_HA         MBps per HA pair, one of 1536|3072|6144 (default: 3072)
#   ONTAP_HA_PAIRS            Number of HA pairs, 1-12 (default: 1)
#   Both disabled → StorageStack is skipped entirely.
#
#   ── License server options ──────────────────────────────────────────
#   ENABLE_LICENSE_SERVER     1 to create EDA license server EC2 (default: 1)
#   LICENSE_INSTANCE_TYPE     EC2 instance type (default: m7i.large)
#
#   ── Cluster topology ────────────────────────────────────────────────
#   ENABLE_LOGIN_NODE         1 to provision ParallelCluster LoginNodes (default: 1)
#                             0 = no login node; users submit jobs from on-prem directly.
#                             (on-prem: slurm client + same munge.key + matching UID required)
#
#   ── VPC endpoints ───────────────────────────────────────────────────
#   ENABLE_VPC_ENDPOINTS      1 to auto-create required VPC endpoints (default: 1)
#                             Always: logs, cloudformation, ec2, s3, dynamodb
#                             + elasticloadbalancing, autoscaling when ENABLE_LOGIN_NODE=1
#                             + ssm, ssmmessages, ec2messages when ENABLE_SSM=1
#                             Existing endpoints in the VPC are automatically skipped.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
CDK_DIR="${PROJECT_DIR}/cdk"
PCLUSTER_VENV="${PROJECT_DIR}/.pcluster-venv"
PCLUSTER="${PCLUSTER_VENV}/bin/pcluster"
PCLUSTER_VERSION_SERIES="3.15"
CDK_MIN_VERSION="2.1033.0"

# ── Config file loading ─────────────────────────────────────
# Priority: env > config file > in-script default
# Snapshot env-set vars before sourcing the config so they are not overwritten.
# (bash 3.2 compatible — uses individual variables instead of associative arrays)
CONFIG_FILE="${CONFIG:-${PROJECT_DIR}/config/default.env}"
if [[ -f "${CONFIG_FILE}" ]]; then
  # Pre-source snapshot: store value and "was set in env" flag in __BAK_<var> / __SET_<var>
  _CONFIG_KEYS=$(grep -E '^[A-Z_][A-Z0-9_]*=' "${CONFIG_FILE}" | cut -d= -f1 | sort -u)
  for _k in ${_CONFIG_KEYS}; do
    if [[ -n "${!_k+x}" ]]; then
      eval "__BAK_${_k}=\"\${${_k}}\""
      eval "__SET_${_k}=1"
    else
      eval "__SET_${_k}=0"
    fi
  done

  set -a
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
  set +a

  # Restore env-original values (env takes precedence)
  for _k in ${_CONFIG_KEYS}; do
    _was_set="$(eval echo \"\${__SET_${_k}}\")"
    if [[ "${_was_set}" == "1" ]]; then
      eval "${_k}=\"\${__BAK_${_k}}\""
      export "${_k}"
    fi
    unset "__SET_${_k}" "__BAK_${_k}"
  done
  unset _CONFIG_KEYS _k _was_set
fi

# ── Defaults (final fallback when neither config nor env provided a value) ──
REGION="${REGION:-ap-northeast-2}"
CLUSTER_NAME="${CLUSTER_NAME:-hpc-cluster}"
STACK_PREFIX="${STACK_PREFIX:-Eda}"
BASE_STACK="${STACK_PREFIX}Base"
STORAGE_STACK="${STACK_PREFIX}Storage"
LICENSE_STACK="${STACK_PREFIX}LicenseServer"

# ── Colors ───────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()  { echo -e "\n${CYAN}${BOLD}── $* ──${NC}\n"; }

aws_capture() {
  local result_var="$1"
  local description="$2"
  local output
  local stderr_file
  local stderr_output
  shift 2
  stderr_file=$(mktemp "${TMPDIR:-/tmp}/eda-aws.XXXXXX") \
    || error "Unable to create a temporary file"
  if ! output=$("$@" 2>"${stderr_file}"); then
    stderr_output=$(<"${stderr_file}")
    rm -f "${stderr_file}"
    error "${description} failed: ${stderr_output}"
  fi
  if [[ -s "${stderr_file}" ]]; then
    cat "${stderr_file}" >&2
  fi
  rm -f "${stderr_file}"
  printf -v "${result_var}" '%s' "${output}"
}

SECONDS=0
elapsed() { echo "$((SECONDS / 60))m $((SECONDS % 60))s"; }

version_at_least() {
  local current="$1"
  local minimum="$2"
  local current_part
  local minimum_part
  local index
  local IFS=.
  local current_parts
  local minimum_parts
  read -r -a current_parts <<< "${current}"
  read -r -a minimum_parts <<< "${minimum}"
  for index in 0 1 2; do
    current_part="${current_parts[$index]:-0}"
    minimum_part="${minimum_parts[$index]:-0}"
    if ((current_part > minimum_part)); then
      return 0
    elif ((current_part < minimum_part)); then
      return 1
    fi
  done
  return 0
}

cleanup_failed_stack() {
  local stack_name="$1"
  local stack_status="$2"

  if [[ "${stack_status}" == "ROLLBACK_IN_PROGRESS" ]]; then
    warn "${stack_name} is rolling back; waiting for completion."
    aws cloudformation wait stack-rollback-complete \
      --stack-name "${stack_name}" --region "${REGION}" \
      || error "Failed while waiting for ${stack_name} rollback"
    stack_status="ROLLBACK_COMPLETE"
  fi

  if [[ "${stack_status}" == "ROLLBACK_COMPLETE" || "${stack_status}" == "CREATE_FAILED" ]]; then
    warn "${stack_name} is ${stack_status}; deleting the failed initial stack before retry."
    aws cloudformation delete-stack \
      --stack-name "${stack_name}" --region "${REGION}" \
      || error "Failed to delete ${stack_name}"
    aws cloudformation wait stack-delete-complete \
      --stack-name "${stack_name}" --region "${REGION}" \
      || error "Failed while waiting for ${stack_name} deletion"
    info "Deleted failed stack ${stack_name}"
  elif [[ "${stack_status}" == "DELETE_IN_PROGRESS" ]]; then
    warn "${stack_name} is being deleted; waiting for completion."
    aws cloudformation wait stack-delete-complete \
      --stack-name "${stack_name}" --region "${REGION}" \
      || error "Failed while waiting for ${stack_name} deletion"
  elif [[ "${stack_status}" == "ROLLBACK_FAILED" || "${stack_status}" == "DELETE_FAILED" ]]; then
    error "${stack_name} is ${stack_status}. Resolve the CloudFormation stack manually before retrying."
  fi
}

validate_binary_flag() {
  local name="$1"
  local value="$2"
  [[ "${value}" == "0" || "${value}" == "1" ]] \
    || error "${name} must be 0 or 1 (found: ${value})"
}

# ══════════════════════════════════════════════════════════════
step "1/6  Environment Validation"
# ══════════════════════════════════════════════════════════════

if [[ -f "${CONFIG_FILE}" ]]; then
  info "Config file: ${CONFIG_FILE}"
else
  warn "Config file not found: ${CONFIG_FILE} (using defaults + env only)"
fi

for cmd in aws python3 node npm jq; do
  command -v "${cmd}" >/dev/null 2>&1 || error "${cmd} is not installed"
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
  || error "Unable to verify AWS credentials. Please run 'aws configure' first"
CURRENT_REGION=$(aws configure get region 2>/dev/null || echo "not-set")
info "Account: ${ACCOUNT_ID}, Region: ${CURRENT_REGION}"

# boto3 checks AWS_REGION before AWS_DEFAULT_REGION — set both
export AWS_REGION="${REGION}"
export AWS_DEFAULT_REGION="${REGION}"
if [[ "${CURRENT_REGION}" != "${REGION}" ]]; then
  warn "aws CLI default region is '${CURRENT_REGION}' — overriding to ${REGION} for this session"
fi

# CDK CLI
CDK_VERSION=""
if command -v cdk >/dev/null 2>&1; then
  CDK_VERSION=$(cdk --version 2>/dev/null | awk '{print $1}')
fi
if [[ -z "${CDK_VERSION}" ]] || ! version_at_least "${CDK_VERSION}" "${CDK_MIN_VERSION}"; then
  info "Installing a CDK CLI compatible with aws-cdk-lib 2.232.1..."
  npm install -g "aws-cdk@^2.1033.0"
  CDK_VERSION=$(cdk --version 2>/dev/null | awk '{print $1}')
fi
version_at_least "${CDK_VERSION}" "${CDK_MIN_VERSION}" \
  || error "CDK CLI ${CDK_VERSION:-unknown} is older than required ${CDK_MIN_VERSION}"
info "CDK CLI: $(cdk --version 2>/dev/null | head -1)"

# ══════════════════════════════════════════════════════════════
step "2/6  Install CDK Python Dependencies"
# ══════════════════════════════════════════════════════════════

if [[ ! -d "${CDK_DIR}/.venv" ]]; then
  info "Creating Python venv..."
  python3 -m venv "${CDK_DIR}/.venv"
fi

source "${CDK_DIR}/.venv/bin/activate"
pip install -q --upgrade pip
pip install -q -r "${CDK_DIR}/requirements.txt"
info "CDK Python dependencies installed"

# ══════════════════════════════════════════════════════════════
step "3/6  Install pcluster CLI"
# ══════════════════════════════════════════════════════════════

if [[ ! -x "${PCLUSTER_VENV}/bin/python" ]]; then
  info "Creating an isolated pcluster virtual environment..."
  python3 -m venv "${PCLUSTER_VENV}"
fi
INSTALLED_PCLUSTER_VERSION=""
if [[ -x "${PCLUSTER}" ]]; then
  INSTALLED_PCLUSTER_VERSION=$("${PCLUSTER}" version 2>/dev/null | jq -r '.version // empty')
fi
if [[ "${INSTALLED_PCLUSTER_VERSION}" != "${PCLUSTER_VERSION_SERIES}".* ]]; then
  info "Installing pcluster ${PCLUSTER_VERSION_SERIES}.x in its isolated virtual environment (takes 1-2 minutes)..."
  "${PCLUSTER_VENV}/bin/python" -m pip install -q --upgrade pip
  "${PCLUSTER_VENV}/bin/python" -m pip install -q --upgrade \
    "aws-parallelcluster~=${PCLUSTER_VERSION_SERIES}.0"
fi
INSTALLED_PCLUSTER_VERSION=$("${PCLUSTER}" version 2>/dev/null | jq -r '.version // empty')
[[ "${INSTALLED_PCLUSTER_VERSION}" == "${PCLUSTER_VERSION_SERIES}".* ]] \
  || error "pcluster ${PCLUSTER_VERSION_SERIES}.x is required (found: ${INSTALLED_PCLUSTER_VERSION:-unknown})"
info "pcluster CLI: ${INSTALLED_PCLUSTER_VERSION} (${PCLUSTER})"

# ══════════════════════════════════════════════════════════════
step "4/6  CDK Bootstrap + Deploy"
# ══════════════════════════════════════════════════════════════

if [[ "${SKIP_CDK:-0}" == "1" ]]; then
  info "Skipping CDK deployment (SKIP_CDK=1)"
  [[ -f "${CDK_DIR}/outputs.json" ]] || error "outputs.json not found. Please run with SKIP_CDK=0"
else
  # ── Resolve VPC_ID / SUBNET_ID (env > cdk.json) ──
  VPC_ID="${VPC_ID:-$(jq -r '.context["eda:vpc_id"] // ""' "${CDK_DIR}/cdk.json")}"
  SUBNET_ID="${SUBNET_ID:-$(jq -r '.context["eda:subnet_id"] // ""' "${CDK_DIR}/cdk.json")}"
  [[ -n "${VPC_ID}" ]]    || error "VPC_ID is required. Usage: VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh"
  [[ -n "${SUBNET_ID}" ]] || error "SUBNET_ID is required. Usage: VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh"
  info "Using existing VPC: ${VPC_ID}"
  info "Using existing Subnet: ${SUBNET_ID}"

  # ── Storage options (defaults) ──
  ENABLE_OPENZFS="${ENABLE_OPENZFS:-1}"
  ENABLE_ONTAP="${ENABLE_ONTAP:-0}"
  OPENZFS_SIZE_GIB="${OPENZFS_SIZE_GIB:-10240}"
  OPENZFS_THROUGHPUT="${OPENZFS_THROUGHPUT:-2560}"
  ONTAP_SIZE_GIB="${ONTAP_SIZE_GIB:-10240}"
  ONTAP_TPUT_PER_HA="${ONTAP_TPUT_PER_HA:-3072}"
  ONTAP_HA_PAIRS="${ONTAP_HA_PAIRS:-1}"
  validate_binary_flag "ENABLE_OPENZFS" "${ENABLE_OPENZFS}"
  validate_binary_flag "ENABLE_ONTAP" "${ENABLE_ONTAP}"
  if [[ "${ENABLE_OPENZFS}" != "1" && "${ENABLE_ONTAP}" != "1" ]]; then
    warn "Both ENABLE_OPENZFS and ENABLE_ONTAP are 0 — StorageStack will be skipped."
  fi
  info "Storage: OpenZFS=${ENABLE_OPENZFS} (${OPENZFS_SIZE_GIB} GiB, ${OPENZFS_THROUGHPUT} MBps), ONTAP=${ENABLE_ONTAP} (${ONTAP_SIZE_GIB} GiB, ${ONTAP_HA_PAIRS} HA × ${ONTAP_TPUT_PER_HA} MBps)"

  # ── License server options ──
  ENABLE_LICENSE_SERVER="${ENABLE_LICENSE_SERVER:-1}"
  LICENSE_INSTANCE_TYPE="${LICENSE_INSTANCE_TYPE:-m7i.large}"
  validate_binary_flag "ENABLE_LICENSE_SERVER" "${ENABLE_LICENSE_SERVER}"
  info "License server: ENABLE_LICENSE_SERVER=${ENABLE_LICENSE_SERVER} (${LICENSE_INSTANCE_TYPE})"

  # ── Cluster topology ──
  ENABLE_LOGIN_NODE="${ENABLE_LOGIN_NODE:-1}"
  ENABLE_SSM="${ENABLE_SSM:-0}"
  validate_binary_flag "ENABLE_LOGIN_NODE" "${ENABLE_LOGIN_NODE}"
  validate_binary_flag "ENABLE_SSM" "${ENABLE_SSM}"
  if [[ "${ENABLE_LOGIN_NODE}" != "1" ]]; then
    warn "LoginNodes disabled — on-prem clients must have matching Slurm version, munge.key, and UID."
  fi
  info "LoginNodes: ENABLE_LOGIN_NODE=${ENABLE_LOGIN_NODE}"
  info "SSM Session Manager: ENABLE_SSM=${ENABLE_SSM}"

  # ── VPC endpoints ──
  ENABLE_VPC_ENDPOINTS="${ENABLE_VPC_ENDPOINTS:-1}"
  validate_binary_flag "ENABLE_VPC_ENDPOINTS" "${ENABLE_VPC_ENDPOINTS}"
  info "VPC endpoints: ENABLE_VPC_ENDPOINTS=${ENABLE_VPC_ENDPOINTS}"

  # ── Validate subnet belongs to VPC ──
  aws_capture ACTUAL_VPC "Subnet lookup" \
    aws ec2 describe-subnets --subnet-ids "${SUBNET_ID}" --region "${REGION}" \
    --query 'Subnets[0].VpcId' --output text
  [[ "${ACTUAL_VPC}" == "${VPC_ID}" ]] \
    || error "Subnet ${SUBNET_ID} does not belong to VPC ${VPC_ID} (actual: ${ACTUAL_VPC:-not-found})"
  aws_capture SUBNET_AZ "Subnet Availability Zone lookup" \
    aws ec2 describe-subnets --subnet-ids "${SUBNET_ID}" --region "${REGION}" \
    --query 'Subnets[0].AvailabilityZone' --output text
  info "Single-subnet Availability Zone: ${SUBNET_AZ}"
  aws_capture AUTO_PUBLIC_IP "Subnet public IPv4 assignment lookup" \
    aws ec2 describe-subnets --subnet-ids "${SUBNET_ID}" --region "${REGION}" \
    --query 'Subnets[0].MapPublicIpOnLaunch' --output text
  [[ "${AUTO_PUBLIC_IP}" == "False" || "${AUTO_PUBLIC_IP}" == "false" ]] \
    || error "Subnet ${SUBNET_ID} has auto-assign public IPv4 enabled. Disable it before creating an internet-isolated ParallelCluster."

  aws_capture DNS_SUPPORT "VPC DNS support lookup" \
    aws ec2 describe-vpc-attribute --vpc-id "${VPC_ID}" --region "${REGION}" \
    --attribute enableDnsSupport --query 'EnableDnsSupport.Value' --output text
  aws_capture DNS_HOSTNAMES "VPC DNS hostnames lookup" \
    aws ec2 describe-vpc-attribute --vpc-id "${VPC_ID}" --region "${REGION}" \
    --attribute enableDnsHostnames --query 'EnableDnsHostnames.Value' --output text
  [[ "${DNS_SUPPORT}" == "True" || "${DNS_SUPPORT}" == "true" ]] \
    || error "VPC ${VPC_ID} must have DNS resolution (enableDnsSupport) enabled."
  [[ "${DNS_HOSTNAMES}" == "True" || "${DNS_HOSTNAMES}" == "true" ]] \
    || error "VPC ${VPC_ID} must have DNS hostnames (enableDnsHostnames) enabled."

  # ── Validate required EC2 instance types in the selected single AZ ──
  REQUIRED_INSTANCE_TYPES=("m7i.xlarge" "r8i.32xlarge")
  if [[ "${ENABLE_LOGIN_NODE}" == "1" ]]; then
    REQUIRED_INSTANCE_TYPES+=("r7i.2xlarge")
  fi
  if [[ "${ENABLE_LICENSE_SERVER}" == "1" ]]; then
    REQUIRED_INSTANCE_TYPES+=("${LICENSE_INSTANCE_TYPE}")
  fi
  for instance_type in "${REQUIRED_INSTANCE_TYPES[@]}"; do
    aws_capture OFFERING_COUNT "Instance offering lookup for ${instance_type}" \
      aws ec2 describe-instance-type-offerings --region "${REGION}" \
      --location-type availability-zone \
      --filters "Name=location,Values=${SUBNET_AZ}" \
                "Name=instance-type,Values=${instance_type}" \
      --query 'length(InstanceTypeOfferings)' --output text
    [[ "${OFFERING_COUNT}" -gt 0 ]] \
      || error "Instance type ${instance_type} is not offered in ${SUBNET_AZ}. Choose a supported SUBNET_ID or instance type."
  done
  info "Required EC2 instance types are offered in ${SUBNET_AZ}"

  # ── Validate subnet connectivity (AWS API reachability) ──
  aws_capture ROUTE_TABLE_ID "Subnet route table lookup" \
    aws ec2 describe-route-tables --region "${REGION}" \
    --filters "Name=association.subnet-id,Values=${SUBNET_ID}" \
    --query 'RouteTables[0].RouteTableId' --output text
  if [[ -z "${ROUTE_TABLE_ID}" || "${ROUTE_TABLE_ID}" == "None" ]]; then
    # fallback to main RT
    aws_capture ROUTE_TABLE_ID "VPC main route table lookup" \
      aws ec2 describe-route-tables --region "${REGION}" \
      --filters "Name=vpc-id,Values=${VPC_ID}" "Name=association.main,Values=true" \
      --query 'RouteTables[0].RouteTableId' --output text
  fi
  [[ -n "${ROUTE_TABLE_ID}" && "${ROUTE_TABLE_ID}" != "None" ]] \
    || error "No route table found for subnet ${SUBNET_ID}"
  aws_capture HAS_DEFAULT_ROUTE "Default route lookup" \
    aws ec2 describe-route-tables --region "${REGION}" \
    --route-table-ids "${ROUTE_TABLE_ID}" \
    --query 'RouteTables[0].Routes[?DestinationCidrBlock==`0.0.0.0/0`] | length(@)' --output text

  # Per ParallelCluster official guide:
  # https://docs.aws.amazon.com/parallelcluster/latest/ug/aws-parallelcluster-in-a-single-public-subnet-no-internet-v3.html
  # Required: logs, cloudformation, ec2 (Interface) + s3, dynamodb (Gateway)
  # Conditional: elasticloadbalancing, autoscaling — required only when LoginNodes is enabled
  REQUIRED_VPCE=(
    "com.amazonaws.${REGION}.logs"
    "com.amazonaws.${REGION}.cloudformation"
    "com.amazonaws.${REGION}.ec2"
    "com.amazonaws.${REGION}.s3"
    "com.amazonaws.${REGION}.dynamodb"
  )
  if [[ "${ENABLE_LOGIN_NODE:-1}" == "1" ]]; then
    REQUIRED_VPCE+=(
      "com.amazonaws.${REGION}.elasticloadbalancing"
      "com.amazonaws.${REGION}.autoscaling"
    )
  fi
  if [[ "${ENABLE_SSM:-0}" == "1" ]]; then
    REQUIRED_VPCE+=(
      "com.amazonaws.${REGION}.ssm"
      "com.amazonaws.${REGION}.ssmmessages"
      "com.amazonaws.${REGION}.ec2messages"
    )
  fi
  aws_capture EXISTING_VPCE "Existing VPC endpoint lookup" \
    aws ec2 describe-vpc-endpoints --region "${REGION}" \
    --filters "Name=vpc-id,Values=${VPC_ID}" \
    --query 'VpcEndpoints[].ServiceName' --output text

  MISSING_VPCE=()
  for svc in "${REQUIRED_VPCE[@]}"; do
    if ! echo "${EXISTING_VPCE}" | tr '\t' '\n' | grep -qx "${svc}"; then
      MISSING_VPCE+=("${svc}")
    fi
  done

  if [[ "${ENABLE_VPC_ENDPOINTS:-1}" == "1" ]]; then
    for svc in "${MISSING_VPCE[@]}"; do
      case "${svc}" in
        "com.amazonaws.${REGION}.s3"|"com.amazonaws.${REGION}.dynamodb")
          continue
          ;;
      esac
      aws_capture SUPPORTED_AZS "Endpoint Availability Zone lookup for ${svc}" \
        aws ec2 describe-vpc-endpoint-services --region "${REGION}" \
        --service-names "${svc}" \
        --query 'ServiceDetails[0].AvailabilityZones' --output text
      if ! echo "${SUPPORTED_AZS}" | tr '\t' '\n' | grep -qx "${SUBNET_AZ}"; then
        error "Endpoint ${svc} does not support single-subnet AZ ${SUBNET_AZ}. Supported AZs: ${SUPPORTED_AZS:-none}. Choose a SUBNET_ID in a supported AZ or disable the feature that requires this endpoint."
      fi
    done
  fi

  if [[ "${HAS_DEFAULT_ROUTE}" == "0" && ${#MISSING_VPCE[@]} -gt 0 ]]; then
    if [[ "${ENABLE_VPC_ENDPOINTS:-1}" == "1" ]]; then
      info "Subnet has no 0.0.0.0/0 route and is missing ${#MISSING_VPCE[@]} endpoint(s):"
      for svc in "${MISSING_VPCE[@]}"; do echo "    - ${svc}"; done
      info "CDK (${BASE_STACK} stack) will create the missing endpoints."
    else
      warn "Subnet ${SUBNET_ID} has NO default (0.0.0.0/0) route AND is missing VPC endpoints:"
      for svc in "${MISSING_VPCE[@]}"; do echo "    - ${svc}"; done
      warn "ParallelCluster bootstrap will likely fail (EC2/CFN/Logs unreachable)."
      warn "Set ENABLE_VPC_ENDPOINTS=1 to have CDK create them, or add a NAT/TGW route."
      if [[ "${SKIP_CONNECTIVITY_CHECK:-0}" != "1" ]]; then
        error "Aborting. Set SKIP_CONNECTIVITY_CHECK=1 to bypass this check."
      fi
    fi
  elif [[ "${HAS_DEFAULT_ROUTE}" == "0" ]]; then
    info "Subnet has no 0.0.0.0/0 route — relying on VPC endpoints for AWS API access"
  else
    info "Subnet has 0.0.0.0/0 route — AWS API reachable via NAT/IGW/TGW"
  fi

  # ── Invalidate stale cdk.context.json if VPC_ID changed ──
  CTX_FILE="${CDK_DIR}/cdk.context.json"
  if [[ -f "${CTX_FILE}" ]]; then
    CACHED_VPC=$(jq -r 'to_entries[] | select(.key | contains("vpc-provider")) | .value.vpcId // empty' "${CTX_FILE}" 2>/dev/null | head -1)
    if [[ -n "${CACHED_VPC}" && "${CACHED_VPC}" != "${VPC_ID}" ]]; then
      warn "cdk.context.json references a different VPC (${CACHED_VPC}); backing up and removing."
      mv "${CTX_FILE}" "${CTX_FILE}.bak.$(date +%Y%m%d%H%M%S)"
    fi
  fi

  cd "${CDK_DIR}"
  export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1

  info "Stack prefix: ${STACK_PREFIX} → ${BASE_STACK}, ${STORAGE_STACK}, ${LICENSE_STACK}"

  # bootstrap also synthesizes the app, so it must receive exactly the same
  # feature context as deploy.
  CDK_CONTEXT_ARGS=(
    -c "eda:vpc_id=${VPC_ID}"
    -c "eda:subnet_id=${SUBNET_ID}"
    -c "eda:stack_prefix=${STACK_PREFIX}"
    -c "eda:enable_vpc_endpoints=${ENABLE_VPC_ENDPOINTS}"
    -c "eda:enable_login_node=${ENABLE_LOGIN_NODE}"
    -c "eda:enable_ssm=${ENABLE_SSM}"
    -c "eda:enable_openzfs=${ENABLE_OPENZFS}"
    -c "eda:enable_ontap=${ENABLE_ONTAP}"
    -c "eda:openzfs_size_gib=${OPENZFS_SIZE_GIB}"
    -c "eda:openzfs_throughput=${OPENZFS_THROUGHPUT}"
    -c "eda:ontap_size_gib=${ONTAP_SIZE_GIB}"
    -c "eda:ontap_tput_per_ha=${ONTAP_TPUT_PER_HA}"
    -c "eda:ontap_ha_pairs=${ONTAP_HA_PAIRS}"
    -c "eda:enable_license_server=${ENABLE_LICENSE_SERVER}"
    -c "eda:license_instance_type=${LICENSE_INSTANCE_TYPE}"
  )

  # A stack whose initial create rolled back cannot be updated. Remove only
  # failed initial-create stacks, children first; never delete a usable stack.
  aws_capture STACK_SUMMARIES "CloudFormation stack status lookup" \
    aws cloudformation list-stacks --region "${REGION}" --output json
  STACK_CLEANUP_ORDER=()
  if [[ "${ENABLE_LICENSE_SERVER}" == "1" ]]; then
    STACK_CLEANUP_ORDER+=("${LICENSE_STACK}")
  fi
  if [[ "${ENABLE_OPENZFS}" == "1" || "${ENABLE_ONTAP}" == "1" ]]; then
    STACK_CLEANUP_ORDER+=("${STORAGE_STACK}")
  fi
  STACK_CLEANUP_ORDER+=("${BASE_STACK}")
  for stack_name in "${STACK_CLEANUP_ORDER[@]}"; do
    stack_status=$(echo "${STACK_SUMMARIES}" | jq -r --arg name "${stack_name}" \
      '[.StackSummaries[] | select(.StackName == $name and .StackStatus != "DELETE_COMPLETE")][0].StackStatus // empty')
    cleanup_failed_stack "${stack_name}" "${stack_status}"
  done

  info "CDK bootstrap..."
  cdk bootstrap "aws://${ACCOUNT_ID}/${REGION}" \
    "${CDK_CONTEXT_ARGS[@]}"

  info "Starting CDK stack deployment (incremental update)..."
  echo ""

  cdk deploy --all \
    --require-approval never \
    --outputs-file outputs.json \
    "${CDK_CONTEXT_ARGS[@]}"

  [[ -f "${CDK_DIR}/outputs.json" ]] || error "CDK deployment failed: outputs.json was not generated"
  info "CDK deployment complete"
  cd "${PROJECT_DIR}"
fi

echo ""
info "Deployed resources:"
jq -r 'to_entries[] | "  [\(.key)]", (.value | to_entries[] | "    \(.key): \(.value)")' "${CDK_DIR}/outputs.json"
echo ""

# ══════════════════════════════════════════════════════════════
step "5/6  Generate ParallelCluster Configuration"
# ══════════════════════════════════════════════════════════════

OUTPUTS="${CDK_DIR}/outputs.json"
TEMPLATE="${CDK_DIR}/pcluster-config-template.yaml"
CONFIG="${CDK_DIR}/pcluster-config.yaml"

SUBNET_ID=$(jq -r --arg s "${BASE_STACK}" '.[$s].PrimarySubnetId' "${OUTPUTS}")
SG_CLUSTER=$(jq -r --arg s "${BASE_STACK}" '.[$s].SgClusterNodesId' "${OUTPUTS}")
KEY_PAIR_NAME=$(jq -r --arg s "${BASE_STACK}" '.[$s].KeyPairName' "${OUTPUTS}")
KEY_PAIR_ID=$(jq -r --arg s "${BASE_STACK}" '.[$s].KeyPairId' "${OUTPUTS}")

for var_name in SUBNET_ID SG_CLUSTER KEY_PAIR_NAME KEY_PAIR_ID; do
  val="${!var_name}"
  [[ "${val}" != "null" && -n "${val}" ]] || error "Could not find ${var_name} value in outputs.json (stack: ${BASE_STACK})"
done

# Storage volume IDs (optional — default empty to allow sed placeholder replace w/o error)
VOL_TOOLS=$(jq -r --arg s "${STORAGE_STACK}" '.[$s].VolToolsId // empty' "${OUTPUTS}")
VOL_WORK=$(jq -r --arg s "${STORAGE_STACK}" '.[$s].VolWorkId // empty' "${OUTPUTS}")
VOL_SCRATCH=$(jq -r --arg s "${STORAGE_STACK}" '.[$s].VolScratchId // empty' "${OUTPUTS}")
ONTAP_TOOLS=$(jq -r --arg s "${STORAGE_STACK}" '.[$s].OntapVolToolsId // empty' "${OUTPUTS}")
ONTAP_WORK=$(jq -r --arg s "${STORAGE_STACK}" '.[$s].OntapVolWorkId // empty' "${OUTPUTS}")
ONTAP_SCRATCH=$(jq -r --arg s "${STORAGE_STACK}" '.[$s].OntapVolScratchId // empty' "${OUTPUTS}")

if [[ "${ENABLE_OPENZFS:-1}" == "1" ]]; then
  for v in VOL_TOOLS VOL_WORK VOL_SCRATCH; do
    [[ -n "${!v}" ]] || error "OpenZFS enabled but ${v} missing in outputs.json"
  done
fi
if [[ "${ENABLE_ONTAP:-0}" == "1" ]]; then
  for v in ONTAP_TOOLS ONTAP_WORK ONTAP_SCRATCH; do
    [[ -n "${!v}" ]] || error "ONTAP enabled but ${v} missing in outputs.json"
  done
fi

sed \
  -e "s|\${REGION}|${REGION}|g" \
  -e "s|\${BASE.PrimarySubnetId}|${SUBNET_ID}|g" \
  -e "s|\${BASE.SgClusterNodesId}|${SG_CLUSTER}|g" \
  -e "s|\${BASE.KeyPairName}|${KEY_PAIR_NAME}|g" \
  -e "s|\${STORAGE.VolToolsId}|${VOL_TOOLS}|g" \
  -e "s|\${STORAGE.VolWorkId}|${VOL_WORK}|g" \
  -e "s|\${STORAGE.VolScratchId}|${VOL_SCRATCH}|g" \
  -e "s|\${STORAGE.OntapVolToolsId}|${ONTAP_TOOLS}|g" \
  -e "s|\${STORAGE.OntapVolWorkId}|${ONTAP_WORK}|g" \
  -e "s|\${STORAGE.OntapVolScratchId}|${ONTAP_SCRATCH}|g" \
  "${TEMPLATE}" > "${CONFIG}"

# OpenZFS / ONTAP block toggling (marker-based)
if [[ "${ENABLE_OPENZFS:-1}" == "1" ]]; then
  info "OpenZFS SharedStorage block: kept"
  sed -i.bak -e '/^  #OPENZFS_BEGIN$/d' -e '/^  #OPENZFS_END$/d' "${CONFIG}"
else
  info "OpenZFS SharedStorage block: removed"
  sed -i.bak -e '/^  #OPENZFS_BEGIN$/,/^  #OPENZFS_END$/d' "${CONFIG}"
fi
if [[ "${ENABLE_ONTAP:-0}" == "1" ]]; then
  info "ONTAP SharedStorage block: kept"
  sed -i.bak -e '/^  #ONTAP_BEGIN$/d' -e '/^  #ONTAP_END$/d' "${CONFIG}"
else
  info "ONTAP SharedStorage block: removed"
  sed -i.bak -e '/^  #ONTAP_BEGIN$/,/^  #ONTAP_END$/d' "${CONFIG}"
fi

# LoginNodes block toggling (column 0 marker)
if [[ "${ENABLE_LOGIN_NODE:-1}" == "1" ]]; then
  info "LoginNodes block: kept"
  sed -i.bak -e '/^#LOGINNODES_BEGIN$/d' -e '/^#LOGINNODES_END$/d' "${CONFIG}"
else
  info "LoginNodes block: removed"
  sed -i.bak -e '/^#LOGINNODES_BEGIN$/,/^#LOGINNODES_END$/d' "${CONFIG}"
fi
rm -f "${CONFIG}.bak"

# SSM enablement handling
if [[ "${ENABLE_SSM:-0}" == "1" ]]; then
  info "SSM Session Manager enabled"
  sed -i.bak \
    -e '/^  #SSM_BEGIN/d' \
    -e '/^  #SSM_END/d' \
    -e 's/^  #\(Iam:\)/  \1/' \
    -e 's/^  #\(  AdditionalIamPolicies:\)/  \1/' \
    -e 's/^  #\(    - Policy:.*\)/  \1/' \
    "${CONFIG}"
  rm -f "${CONFIG}.bak"
else
  info "SSM Session Manager disabled (default)"
  sed -i.bak \
    -e '/^  #SSM_BEGIN/d' \
    -e '/^  #SSM_END/d' \
    -e '/^  #Iam:/d' \
    -e '/^  #  AdditionalIamPolicies:/d' \
    -e '/^  #    - Policy:.*SSM/d' \
    "${CONFIG}"
  rm -f "${CONFIG}.bak"
fi

# If both storages disabled, remove the dangling `SharedStorage:` header (Optional section)
if [[ "${ENABLE_OPENZFS:-1}" != "1" && "${ENABLE_ONTAP:-0}" != "1" ]]; then
  warn "No shared storage configured. Removing SharedStorage: header."
  sed -i.bak -e '/^SharedStorage:[[:space:]]*$/d' "${CONFIG}"
  rm -f "${CONFIG}.bak"
fi

# Check for unresolved placeholders (excluding comments)
if grep -v '^#' "${CONFIG}" | grep -q '${'; then
  warn "Unresolved placeholders:"
  grep -v '^#' "${CONFIG}" | grep '${' || true
  error "Unresolved placeholders remain in the configuration file"
fi

info "ParallelCluster configuration file generated: ${CONFIG}"

# Cluster SSH private key download (always re-download to avoid stale key when KeyPair is recreated)
SSH_KEY_FILE="${HOME}/.ssh/${KEY_PAIR_NAME}.pem"
info "Downloading cluster SSH private key from SSM Parameter Store..."
mkdir -p "${HOME}/.ssh"
aws ssm get-parameter \
  --name "/ec2/keypair/${KEY_PAIR_ID}" \
  --with-decryption \
  --query 'Parameter.Value' \
  --output text \
  --region "${REGION}" > "${SSH_KEY_FILE}" \
  || error "Failed to download cluster SSH private key"
chmod 400 "${SSH_KEY_FILE}"
info "Cluster SSH key saved: ${SSH_KEY_FILE}"

# License Server SSH Private Key + connection info
if [[ "${ENABLE_LICENSE_SERVER:-1}" == "1" ]]; then
  LIC_KEY_NAME=$(jq -r --arg s "${LICENSE_STACK}" '.[$s].LicenseKeyPairName // empty' "${OUTPUTS}")
  LIC_KEY_ID=$(jq -r --arg s "${LICENSE_STACK}" '.[$s].LicenseKeyPairId // empty' "${OUTPUTS}")
  LIC_IP=$(jq -r --arg s "${LICENSE_STACK}" '.[$s].LicensePrivateIp // empty' "${OUTPUTS}")
  LIC_ENI=$(jq -r --arg s "${LICENSE_STACK}" '.[$s].LicenseEniId // empty' "${OUTPUTS}")
  [[ -n "${LIC_KEY_NAME}" && -n "${LIC_KEY_ID}" ]] \
    || error "License server outputs missing. Re-run CDK deploy."

  LIC_KEY_FILE="${HOME}/.ssh/${LIC_KEY_NAME}.pem"
  info "Downloading license server SSH private key from SSM Parameter Store..."
  aws ssm get-parameter \
    --name "/ec2/keypair/${LIC_KEY_ID}" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text \
    --region "${REGION}" > "${LIC_KEY_FILE}" \
    || error "Failed to download license server SSH private key"
  chmod 400 "${LIC_KEY_FILE}"
  info "License server SSH key saved: ${LIC_KEY_FILE}"

  # MAC address (license Host ID) is fetched via describe-network-interfaces
  LIC_MAC=$(aws ec2 describe-network-interfaces \
    --network-interface-ids "${LIC_ENI}" \
    --region "${REGION}" \
    --query 'NetworkInterfaces[0].MacAddress' \
    --output text 2>/dev/null || echo "unknown")

  echo ""
  echo "══════════════════════════════════════════"
  echo "  EDA License Server"
  echo "──────────────────────────────────────────"
  echo "  Private IP:    ${LIC_IP}"
  echo "  MAC address:   ${LIC_MAC}   (= license Host ID)"
  echo "  SSH:           ssh -i ${LIC_KEY_FILE} ec2-user@${LIC_IP}"
  echo ""
  echo "  Cluster-side LM_LICENSE_FILE:"
  echo "    export LM_LICENSE_FILE=27000@${LIC_IP}"
  echo "══════════════════════════════════════════"
  echo ""
fi

# ══════════════════════════════════════════════════════════════
step "6/6  Create ParallelCluster"
# ══════════════════════════════════════════════════════════════

# Display existing cluster list
info "Existing ParallelCluster list:"
CLUSTER_LIST=$("${PCLUSTER}" list-clusters --region "${REGION}" 2>/dev/null \
  | jq -r '.clusters[] | "  \(.clusterName)  (\(.clusterStatus))"' 2>/dev/null || true)
if [[ -n "${CLUSTER_LIST}" ]]; then
  echo "${CLUSTER_LIST}"
else
  echo "  (none)"
fi
echo ""

if [[ "${SKIP_CLUSTER:-0}" == "1" ]]; then
  warn "SKIP_CLUSTER=1 — Skipping cluster creation"
  echo ""
  info "To create manually:"
  echo "  ${PCLUSTER} create-cluster --cluster-name ${CLUSTER_NAME} --cluster-configuration ${CONFIG} --region ${REGION}"
  echo ""
  info "Total elapsed time: $(elapsed)"
  exit 0
fi

# Check if a cluster with the same name already exists
EXISTING=$("${PCLUSTER}" describe-cluster --cluster-name "${CLUSTER_NAME}" --region "${REGION}" 2>/dev/null \
  | jq -r '.clusterStatus // empty' 2>/dev/null || true)

if [[ -n "${EXISTING}" ]]; then
  case "${EXISTING}" in
    CREATE_FAILED|ROLLBACK_COMPLETE)
      warn "Cluster '${CLUSTER_NAME}' is ${EXISTING}; deleting the failed initial cluster before retry."
      "${PCLUSTER}" delete-cluster \
        --cluster-name "${CLUSTER_NAME}" \
        --region "${REGION}"
      aws cloudformation wait stack-delete-complete \
        --stack-name "${CLUSTER_NAME}" \
        --region "${REGION}" \
        || error "Failed while waiting for cluster stack ${CLUSTER_NAME} deletion"
      info "Deleted failed cluster ${CLUSTER_NAME}"
      ;;
    DELETE_IN_PROGRESS)
      warn "Cluster '${CLUSTER_NAME}' is being deleted; waiting before retry."
      aws cloudformation wait stack-delete-complete \
        --stack-name "${CLUSTER_NAME}" \
        --region "${REGION}" \
        || error "Failed while waiting for cluster stack ${CLUSTER_NAME} deletion"
      ;;
    CREATE_IN_PROGRESS)
      warn "Cluster '${CLUSTER_NAME}' creation is already in progress; resuming monitoring."
      ;;
    CREATE_COMPLETE|UPDATE_COMPLETE)
      warn "Cluster '${CLUSTER_NAME}' already exists (status: ${EXISTING})"
      info "Total elapsed time: $(elapsed)"
      exit 0
      ;;
    *)
      error "Cluster '${CLUSTER_NAME}' already exists in status ${EXISTING}. Resolve it before retrying."
      ;;
  esac
fi

if [[ "${EXISTING}" != "CREATE_IN_PROGRESS" ]]; then
  info "Starting ParallelCluster creation: ${CLUSTER_NAME}"
  "${PCLUSTER}" create-cluster \
    --cluster-name "${CLUSTER_NAME}" \
    --cluster-configuration "${CONFIG}" \
    --region "${REGION}"
fi

echo ""
info "Cluster creation started (takes 10-15 minutes)"
info "Monitoring status..."
echo ""

# Poll locally until completion
while true; do
  STATUS=$("${PCLUSTER}" describe-cluster --cluster-name "${CLUSTER_NAME}" --region "${REGION}" \
    | jq -r '.clusterStatus' 2>/dev/null || echo "UNKNOWN")
  TIMESTAMP=$(date '+%H:%M:%S')

  case "${STATUS}" in
    CREATE_COMPLETE)
      echo ""
      info "Cluster creation complete!"

      CLUSTER_INFO=$("${PCLUSTER}" describe-cluster --cluster-name "${CLUSTER_NAME}" --region "${REGION}")
      HEAD_IP=$(echo "${CLUSTER_INFO}" | jq -r '.headNode.privateIpAddress // "N/A"')
      LOGIN_ADDR=$(echo "${CLUSTER_INFO}" | jq -r '.loginNodes[0].address // empty')

      echo ""
      echo "══════════════════════════════════════════"
      echo "  Cluster:     ${CLUSTER_NAME}"
      echo "  Head Node:   ${HEAD_IP} (private)"
      if [[ -n "${LOGIN_ADDR}" ]]; then
        echo "  Login Node:  ${LOGIN_ADDR}"
        echo ""
        echo "  Login Node access (via VPN):"
        echo "    ssh -i ${SSH_KEY_FILE} ec2-user@${LOGIN_ADDR}"
      else
        echo "  Login Node:  (disabled — on-prem client submission)"
        echo ""
        echo "  To submit from on-prem PC, ensure:"
        echo "    - Same Slurm version as cluster (RHEL 8 recommended)"
        echo "    - /etc/munge/munge.key copied from head node"
        echo "    - UID/GID matches cluster user account"
        echo "    - Head node reachable on TCP 6817 via VPN"
      fi
      echo ""
      echo "  Head Node access:"
      echo "    ${PCLUSTER} ssh --cluster-name ${CLUSTER_NAME} --region ${REGION} -i ${SSH_KEY_FILE}"
      echo ""
      echo "  Delete:"
      echo "    ${PCLUSTER} delete-cluster --cluster-name ${CLUSTER_NAME} --region ${REGION}"
      echo "══════════════════════════════════════════"
      echo ""
      info "Total elapsed time: $(elapsed)"
      echo -e "${GREEN}${BOLD}Setup complete!${NC}"
      exit 0
      ;;
    CREATE_FAILED|ROLLBACK_COMPLETE|ROLLBACK_IN_PROGRESS|DELETE_*)
      echo ""
      error "Cluster creation failed (status: ${STATUS})\n  ${PCLUSTER} describe-cluster --cluster-name ${CLUSTER_NAME} --region ${REGION}"
      ;;
    *)
      echo -ne "\r  [${TIMESTAMP}] Status: ${STATUS}  "
      sleep 30
      ;;
  esac
done
