#!/usr/bin/env bash
set -euo pipefail

PCS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${PCS_DIR}/.." && pwd)"
ACTION="${1:-deploy}"

if [[ "${ACTION}" == "destory" ]]; then
  ACTION="destroy"
fi

case "${ACTION}" in
  synth|diff|deploy|destroy|foundation-synth|foundation-diff|foundation-deploy) ;;
  *)
    echo "Usage: $0 [synth|diff|deploy|destroy|foundation-synth|foundation-diff|foundation-deploy]" >&2
    exit 2
    ;;
esac

ROOT_DEFAULT_CONFIG="${PROJECT_DIR}/config/default.env"
PCS_DEFAULT_CONFIG="${PCS_DIR}/config/default.env"
CONFIG="${CONFIG:-${ROOT_DEFAULT_CONFIG}}"
PCS_CONFIG="${PCS_CONFIG:-${PCS_DEFAULT_CONFIG}}"

require_value() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required configuration: ${name}" >&2
    exit 1
  fi
}

if [[ "${ACTION}" == foundation-* ]]; then
  # Foundation actions intentionally use the existing shared CDK profile.
  set -a
  source "${ROOT_DEFAULT_CONFIG}"
  if [[ "${CONFIG}" != "${ROOT_DEFAULT_CONFIG}" ]]; then
    source "${CONFIG}"
  fi
  source "${PCS_DEFAULT_CONFIG}"
  if [[ "${PCS_CONFIG}" != "${PCS_DEFAULT_CONFIG}" ]]; then
    source "${PCS_CONFIG}"
  fi
  set +a

  for name in VPC_ID SUBNET_ID; do
    require_value "${name}"
  done
  REGION="${REGION:-ap-northeast-2}"
  STACK_PREFIX="${STACK_PREFIX:-Eda}"
else
  # Regular PCS actions are independent of the ParallelCluster configuration.
  set -a
  source "${PCS_DEFAULT_CONFIG}"
  if [[ "${PCS_CONFIG}" != "${PCS_DEFAULT_CONFIG}" ]]; then
    source "${PCS_CONFIG}"
  fi
  set +a

  require_value PCS_AMI_ID
  REGION="${PCS_REGION:-${AWS_REGION:-ap-northeast-2}}"
fi

for command in aws node npm python3; do
  command -v "${command}" >/dev/null 2>&1 || {
    echo "Required command not found: ${command}" >&2
    exit 1
  }
done

export AWS_REGION="${REGION}"
export AWS_DEFAULT_REGION="${REGION}"
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_ACCOUNT="${ACCOUNT_ID}"
export CDK_DEFAULT_REGION="${REGION}"

if [[ ! -x "${PCS_DIR}/node_modules/.bin/cdk" ]]; then
  npm install --prefix "${PCS_DIR}" --no-audit --no-fund
fi
CDK="${PCS_DIR}/node_modules/.bin/cdk"

if [[ "${ACTION}" == foundation-* ]]; then
  shared_context=(
    -c "eda:vpc_id=${VPC_ID}"
    -c "eda:subnet_id=${SUBNET_ID}"
    -c "eda:stack_prefix=${STACK_PREFIX}"
    -c "eda:enable_vpc_endpoints=${ENABLE_VPC_ENDPOINTS}"
    -c "eda:enable_login_node=${ENABLE_LOGIN_NODE}"
    -c "eda:enable_ssm=${PCS_ENABLE_SSM}"
    -c "eda:enable_openzfs=${ENABLE_OPENZFS}"
    -c "eda:enable_ontap=${ENABLE_ONTAP}"
    -c "eda:openzfs_size_gib=${OPENZFS_SIZE_GIB}"
    -c "eda:openzfs_throughput=${OPENZFS_THROUGHPUT}"
    -c "eda:openzfs_iops=${OPENZFS_IOPS}"
    -c "eda:ontap_size_gib=${ONTAP_SIZE_GIB}"
    -c "eda:ontap_tput_per_ha=${ONTAP_TPUT_PER_HA}"
    -c "eda:ontap_ha_pairs=${ONTAP_HA_PAIRS}"
    -c "eda:license_instance_type=${LICENSE_INSTANCE_TYPE}"
    -c "eda:license_manager_port=${LICENSE_MANAGER_PORT}"
    -c "eda:license_vendor_port=${LICENSE_VENDOR_PORT}"
  )

  if [[ ! -d "${PROJECT_DIR}/cdk/.venv" ]]; then
    python3 -m venv "${PROJECT_DIR}/cdk/.venv"
  fi
  "${PROJECT_DIR}/cdk/.venv/bin/pip" install -q \
    -r "${PROJECT_DIR}/cdk/requirements.txt"

  case "${PCS_FOUNDATION_SCOPE:-base}" in
    base)
      foundation_targets=("${STACK_PREFIX}Base")
      ;;
    all)
      foundation_targets=(--all)
      ;;
    *)
      echo "PCS_FOUNDATION_SCOPE must be base or all" >&2
      exit 1
      ;;
  esac
  if [[
    "${ACTION}" == "foundation-deploy"
    && "${PCS_FOUNDATION_SCOPE:-base}" == "all"
    && "${PCS_CONFIRM_DEPLOY_ALL:-0}" != "1"
  ]]; then
    echo "Set PCS_CONFIRM_DEPLOY_ALL=1 to create or update all shared foundation stacks." >&2
    exit 1
  fi

  (
    cd "${PROJECT_DIR}/cdk"
    case "${ACTION}" in
      foundation-synth)
        "${CDK}" synth "${foundation_targets[@]}" "${shared_context[@]}"
        ;;
      foundation-diff)
        "${CDK}" diff "${foundation_targets[@]}" "${shared_context[@]}"
        ;;
      foundation-deploy)
        "${CDK}" bootstrap "aws://${ACCOUNT_ID}/${REGION}" "${shared_context[@]}"
        "${CDK}" deploy "${foundation_targets[@]}" \
          --require-approval broadening \
          --outputs-file outputs.json \
          "${shared_context[@]}"
        ;;
    esac
  )
  echo "Shared foundation ${ACTION#foundation-} completed with scope ${PCS_FOUNDATION_SCOPE:-base}."
  exit 0
fi

if [[ ! -d "${PCS_DIR}/.venv" ]]; then
  python3 -m venv "${PCS_DIR}/.venv"
fi
"${PCS_DIR}/.venv/bin/pip" install -q \
  -r "${PCS_DIR}/requirements.txt" \
  -r "${PCS_DIR}/requirements-dev.txt"

resolve_storage=1
if [[ "${ACTION}" == "destroy" ]]; then
  resolve_storage=0
fi
resolved_runtime_config=$(
  PCS_RESOLVE_STORAGE="${resolve_storage}" \
    "${PCS_DIR}/.venv/bin/python" \
    "${PCS_DIR}/scripts/resolve_runtime_config.py"
)
while IFS=$'\t' read -r name value; do
  [[ -z "${name}" ]] && continue
  case "${name}" in
    PCS_VPC_ID|PCS_SUBNET_ID|PCS_KEY_PAIR_NAME|PCS_OPENZFS_DNS|PCS_OPENZFS_SECURITY_GROUP_ID|PCS_ONTAP_SVM_NFS_DNS|PCS_ONTAP_SECURITY_GROUP_ID|PCS_LICENSE_SECURITY_GROUP_ID|PCS_LICENSE_MANAGER_PORT|PCS_LICENSE_VENDOR_PORT)
      printf -v "${name}" '%s' "${value}"
      export "${name}"
      echo "Resolved ${name}=${value} from Shared Foundation CloudFormation or configured FSx."
      ;;
    *)
      echo "Unexpected resolved configuration key: ${name}" >&2
      exit 1
      ;;
  esac
done <<< "${resolved_runtime_config}"

for name in PCS_VPC_ID PCS_SUBNET_ID; do
  require_value "${name}"
done

if [[
  ("${ACTION}" == "diff" || "${ACTION}" == "deploy")
  && "${PCS_SKIP_PREFLIGHT:-0}" != "1"
]]; then
  "${PCS_DIR}/.venv/bin/python" "${PCS_DIR}/scripts/preflight.py"
fi

pcs_context=(
  -c "pcs:stack_name=${PCS_STACK_NAME}"
  -c "pcs:shared_stack_prefix=${PCS_SHARED_STACK_PREFIX}"
  -c "pcs:cluster_name=${PCS_CLUSTER_NAME}"
  -c "pcs:cluster_size=${PCS_CLUSTER_SIZE}"
  -c "pcs:slurm_version=${PCS_SLURM_VERSION}"
  -c "pcs:vpc_id=${PCS_VPC_ID}"
  -c "pcs:subnet_id=${PCS_SUBNET_ID}"
  -c "pcs:ami_id=${PCS_AMI_ID}"
  -c "pcs:root_device_name=${PCS_ROOT_DEVICE_NAME}"
  -c "pcs:login_instance_type=${PCS_LOGIN_INSTANCE_TYPE}"
  -c "pcs:compute_instance_types=${PCS_COMPUTE_INSTANCE_TYPES}"
  -c "pcs:compute_min_count=${PCS_COMPUTE_MIN_COUNT}"
  -c "pcs:compute_max_count=${PCS_COMPUTE_MAX_COUNT}"
  -c "pcs:queue_name=${PCS_QUEUE_NAME}"
  -c "pcs:purchase_option=${PCS_PURCHASE_OPTION}"
  -c "pcs:spot_allocation_strategy=${PCS_SPOT_ALLOCATION_STRATEGY}"
  -c "pcs:enable_login_node=${PCS_ENABLE_LOGIN_NODE}"
  -c "pcs:ssh_cidr=${PCS_SSH_CIDR}"
  -c "pcs:enable_ssm=${PCS_ENABLE_SSM}"
  -c "pcs:key_pair_name=${PCS_KEY_PAIR_NAME}"
  -c "pcs:enable_license_access=${PCS_ENABLE_LICENSE_ACCESS}"
  -c "pcs:license_security_group_id=${PCS_LICENSE_SECURITY_GROUP_ID}"
  -c "pcs:license_manager_port=${PCS_LICENSE_MANAGER_PORT}"
  -c "pcs:license_vendor_port=${PCS_LICENSE_VENDOR_PORT}"
  -c "pcs:enable_accounting=${PCS_ENABLE_ACCOUNTING}"
  -c "pcs:accounting_purge_days=${PCS_ACCOUNTING_PURGE_DAYS}"
  -c "pcs:scale_down_idle_seconds=${PCS_SCALE_DOWN_IDLE_SECONDS}"
  -c "pcs:enable_openzfs_mounts=${PCS_ENABLE_OPENZFS_MOUNTS}"
  -c "pcs:openzfs_dns=${PCS_OPENZFS_DNS}"
  -c "pcs:openzfs_security_group_id=${PCS_OPENZFS_SECURITY_GROUP_ID}"
  -c "pcs:enable_ontap_mounts=${PCS_ENABLE_ONTAP_MOUNTS}"
  -c "pcs:ontap_svm_nfs_dns=${PCS_ONTAP_SVM_NFS_DNS}"
  -c "pcs:ontap_security_group_id=${PCS_ONTAP_SECURITY_GROUP_ID}"
  -c "pcs:enable_local_scratch=${PCS_ENABLE_LOCAL_SCRATCH}"
  -c "pcs:local_scratch_mount_point=${PCS_LOCAL_SCRATCH_MOUNT_POINT}"
  -c "pcs:enable_cloudwatch_lifecycle_logs=${PCS_ENABLE_CLOUDWATCH_LIFECYCLE_LOGS}"
  -c "pcs:enable_scheduler_log_delivery=${PCS_ENABLE_SCHEDULER_LOG_DELIVERY}"
  -c "pcs:enable_job_completion_log_delivery=${PCS_ENABLE_JOB_COMPLETION_LOG_DELIVERY}"
  -c "pcs:enable_scheduler_audit_log_delivery=${PCS_ENABLE_SCHEDULER_AUDIT_LOG_DELIVERY}"
  -c "pcs:log_retention_days=${PCS_LOG_RETENTION_DAYS}"
  -c "pcs:create_pcs_vpc_endpoint=${PCS_CREATE_PCS_VPC_ENDPOINT}"
  -c "pcs:login_root_volume_gib=${PCS_LOGIN_ROOT_VOLUME_GIB}"
  -c "pcs:compute_root_volume_gib=${PCS_COMPUTE_ROOT_VOLUME_GIB}"
)

(
  cd "${PCS_DIR}"
  "${CDK}" synth "${pcs_context[@]}"
  case "${ACTION}" in
    synth)
      ;;
    diff)
      "${CDK}" diff "${pcs_context[@]}"
      ;;
    deploy)
      "${CDK}" bootstrap "aws://${ACCOUNT_ID}/${REGION}" "${pcs_context[@]}"
      "${CDK}" deploy "${PCS_STACK_NAME}" \
        --require-approval never \
        --outputs-file outputs.json \
        "${pcs_context[@]}"
      ;;
    destroy)
      "${CDK}" destroy "${PCS_STACK_NAME}" \
        --force \
        "${pcs_context[@]}"
      ;;
  esac
)

echo "AWS PCS ${ACTION} completed for ${PCS_CLUSTER_NAME} in ${REGION}."
