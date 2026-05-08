#!/usr/bin/env bash
#
# submit.sh — 로컬에서 ParallelCluster로 sbatch 제출
#
# 기본적으로 Login Node(ALB DNS)로 접속합니다. Head Node IP를 써도 동일하게 동작.
#
# Usage:
#   ./submit.sh                          # hello.sbatch 제출
#   ./submit.sh path/to/other.sbatch     # 다른 job script
#
# Required env:
#   REMOTE_HOST      Login Node ALB DNS (권장) 또는 Head Node private IP
#                    예: hpc-cl-xxxx.elb.ap-northeast-2.amazonaws.com, 10.0.10.35
#
# Optional env:
#   SSH_KEY          pem 경로 (default: ~/.ssh/eda-cluster-key-<AWS_ACCOUNT>.pem
#                    where AWS_ACCOUNT = aws sts get-caller-identity --query Account)
#   REMOTE_USER      default: ec2-user
#   REMOTE_DIR       default: /fsxz/work/jobs/<USER>/<job-name>-<timestamp>
#
# Flow:
#   1. /fsxz/work 아래에 job 디렉터리 생성 (ssh mkdir)
#   2. job script + 주변 파일 rsync (현재 스크립트 디렉터리 전체)
#   3. sbatch 제출 → jobid 파싱
#   4. squeue로 상태 polling → 완료 후 .out/.err tail
#   5. rsync로 결과를 로컬 results/ 로 회수
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
JOB_SCRIPT="${1:-${SCRIPT_DIR}/hello.sbatch}"

# Login Node ALB DNS 또는 Head Node private IP — 환경변수로 지정
REMOTE_HOST="${REMOTE_HOST:-${HEAD_IP:-}}"
if [[ -z "${REMOTE_HOST}" ]]; then
  echo "ERROR: REMOTE_HOST is not set. Usage:" >&2
  echo "  REMOTE_HOST=<login-node-alb-dns-or-head-ip> $0" >&2
  exit 1
fi

# SSH key: 명시 안하면 AWS 계정으로부터 자동 추정
if [[ -z "${SSH_KEY:-}" ]]; then
  AWS_ACCOUNT="${AWS_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo '')}"
  [[ -n "${AWS_ACCOUNT}" ]] || { echo "ERROR: Set SSH_KEY or ensure AWS credentials are configured." >&2; exit 1; }
  SSH_KEY="${HOME}/.ssh/eda-cluster-key-${AWS_ACCOUNT}.pem"
fi
REMOTE_USER="${REMOTE_USER:-ec2-user}"
JOB_NAME="$(basename "${JOB_SCRIPT}" .sbatch)"
REMOTE_DIR="${REMOTE_DIR:-/fsxz/work/jobs/${REMOTE_USER}/${JOB_NAME}-$(date +%Y%m%d-%H%M%S)}"

SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new"

echo "── Local job script: ${JOB_SCRIPT}"
echo "── Remote workdir:   ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"

# 1) create remote dir
${SSH} "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR}"

# 2) upload the whole job dir (so .sbatch + inputs go together)
rsync -az -e "ssh -i ${SSH_KEY}" \
  --exclude 'results/' --exclude '*.out' --exclude '*.err' \
  "${SCRIPT_DIR}/" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

# 3) submit
REMOTE_SCRIPT="${REMOTE_DIR}/$(basename "${JOB_SCRIPT}")"
SBATCH_OUT=$(${SSH} "${REMOTE_USER}@${REMOTE_HOST}" \
  "cd ${REMOTE_DIR} && sbatch --parsable ${REMOTE_SCRIPT}")
JOB_ID=$(echo "${SBATCH_OUT}" | awk -F';' '{print $1}' | tr -d '[:space:]')
[[ -n "${JOB_ID}" ]] || { echo "sbatch failed: ${SBATCH_OUT}"; exit 1; }
echo "── Submitted job ${JOB_ID}"

# 4) poll
echo "── Waiting for completion (compute node cold-start ~2–3 min)…"
while true; do
  STATE=$(${SSH} "${REMOTE_USER}@${REMOTE_HOST}" \
    "squeue -h -j ${JOB_ID} -o '%T' 2>/dev/null" || true)
  if [[ -z "${STATE}" ]]; then
    break   # job no longer in queue → completed (or failed)
  fi
  printf "   [%s] %s\n" "$(date +%H:%M:%S)" "${STATE}"
  sleep 15
done

# 5) show sacct result + tail logs + pull back
echo
echo "── sacct summary:"
${SSH} "${REMOTE_USER}@${REMOTE_HOST}" \
  "sacct -j ${JOB_ID} --format=JobID,JobName,Partition,State,ExitCode,Elapsed,MaxRSS" || true

echo
echo "── stdout (hello-${JOB_ID}.out):"
${SSH} "${REMOTE_USER}@${REMOTE_HOST}" \
  "cat ${REMOTE_DIR}/${JOB_NAME}-${JOB_ID}.out 2>/dev/null" || true

STDERR_CONTENT=$(${SSH} "${REMOTE_USER}@${REMOTE_HOST}" \
  "cat ${REMOTE_DIR}/${JOB_NAME}-${JOB_ID}.err 2>/dev/null" || true)
if [[ -n "${STDERR_CONTENT}" ]]; then
  echo
  echo "── stderr (hello-${JOB_ID}.err):"
  echo "${STDERR_CONTENT}"
fi

LOCAL_RESULTS="${SCRIPT_DIR}/results/${JOB_NAME}-${JOB_ID}"
mkdir -p "${LOCAL_RESULTS}"
rsync -az -e "ssh -i ${SSH_KEY}" \
  --include='*.out' --include='*.err' --exclude='*' \
  "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/" "${LOCAL_RESULTS}/" || true
echo
echo "── Results copied to: ${LOCAL_RESULTS}"
