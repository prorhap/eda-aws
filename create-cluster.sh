#!/usr/bin/env bash
#
# Recreate only the ParallelCluster using the same validated path as setup.sh.
#
# Usage:
#   ./create-cluster.sh
#   CONFIG=config/prod.env ./create-cluster.sh
#   CLUSTER_NAME=eda-dev ./create-cluster.sh
#   DRY_RUN=1 ./create-cluster.sh
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  export SKIP_CLUSTER=1
fi
export SKIP_CDK=1
exec "${PROJECT_DIR}/setup.sh"
