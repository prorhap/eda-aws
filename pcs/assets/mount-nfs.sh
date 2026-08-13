#!/usr/bin/env bash
set -euo pipefail

source_path=""
mount_point=""
mount_options="nfsvers=3,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      source_path="$2"
      shift 2
      ;;
    --mount-point)
      mount_point="$2"
      shift 2
      ;;
    --options)
      mount_options="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "${source_path}" || -z "${mount_point}" ]]; then
  echo "--source and --mount-point are required" >&2
  exit 2
fi

mkdir -p "${mount_point}"

if mountpoint -q "${mount_point}"; then
  current_source=$(findmnt -n -o SOURCE --target "${mount_point}")
  if [[ "${current_source}" == "${source_path}" ]]; then
    exit 0
  fi
  echo "${mount_point} is already mounted from ${current_source}" >&2
  exit 1
fi

if ! command -v mount.nfs >/dev/null 2>&1; then
  echo "NFS client tools are missing from the PCS AMI" >&2
  exit 1
fi

fstab_line="${source_path} ${mount_point} nfs ${mount_options},_netdev 0 0"
if ! grep -Fqs "${source_path} ${mount_point} nfs " /etc/fstab; then
  printf '%s\n' "${fstab_line}" >> /etc/fstab
fi

mount "${mount_point}"
mountpoint -q "${mount_point}"
