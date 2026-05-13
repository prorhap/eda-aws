#!/usr/bin/env bash
#
# Destroy a personal DCV instance
#
# Usage:
#   ./destroy-dcv.sh <username>
#
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

USERNAME="${1:-}"
[[ -n "${USERNAME}" ]] || error "Usage: $0 <username>"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

source .venv/bin/activate 2>/dev/null || true

info "Destroying DCV instance for: ${USERNAME}"

cdk destroy "EdaDcv-${USERNAME}" \
  -c "eda:dcv_username=${USERNAME}" \
  --force

rm -f "outputs-${USERNAME}.json"
info "DCV instance for ${USERNAME} destroyed"
