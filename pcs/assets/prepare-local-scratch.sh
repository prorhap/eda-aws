#!/usr/bin/env bash
set -euo pipefail

mount_point="/local_scratch"
raid_device="/dev/md/local_scratch"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mount-point)
      mount_point="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

for command in findmnt lsblk mdadm mkfs.xfs mount; do
  command -v "${command}" >/dev/null 2>&1 || {
    echo "Required local scratch command is missing: ${command}" >&2
    exit 1
  }
done

if findmnt --mountpoint "${mount_point}" >/dev/null 2>&1; then
  chmod 1777 "${mount_point}"
  exit 0
fi

devices=()
for sys_device in /sys/block/nvme*n1; do
  [[ -e "${sys_device}/device/model" ]] || continue
  model=$(tr -d ' ' < "${sys_device}/device/model")
  if [[ "${model}" == *AmazonEC2NVMeInstanceStorage* ]]; then
    devices+=("/dev/${sys_device##*/}")
  fi
done

if [[ ${#devices[@]} -eq 0 ]]; then
  echo "No EC2 NVMe instance-store devices were found" >&2
  exit 1
fi

if [[ ${#devices[@]} -eq 1 ]]; then
  scratch_device="${devices[0]}"
else
  mkdir -p /dev/md
  if [[ ! -e "${raid_device}" ]]; then
    if ! mdadm --assemble "${raid_device}" "${devices[@]}"; then
      mdadm --create "${raid_device}" \
        --metadata=1.2 \
        --level=0 \
        --raid-devices="${#devices[@]}" \
        --chunk=256 \
        --force \
        "${devices[@]}"
    fi
  fi
  scratch_device="${raid_device}"
fi

if ! lsblk -no FSTYPE "${scratch_device}" | grep -q .; then
  mkfs.xfs -f -L local_scratch "${scratch_device}"
fi

mkdir -p "${mount_point}"
mount -o noatime,nodiratime "${scratch_device}" "${mount_point}"
chmod 1777 "${mount_point}"
