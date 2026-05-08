#!/bin/bash
#SBATCH --job-name=hello
#SBATCH --partition=eda-r8i
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --time=00:05:00
#SBATCH --output=hello-%j.out
#SBATCH --error=hello-%j.err

echo "=== Hello from Slurm ==="
echo "Host:         $(hostname)"
echo "Date:         $(date)"
echo "JobID:        ${SLURM_JOB_ID}"
echo "Partition:    ${SLURM_JOB_PARTITION}"
echo "Nodes:        ${SLURM_JOB_NODELIST}"
echo "CPUs/task:    ${SLURM_CPUS_PER_TASK}"
echo "Working dir:  $(pwd)"
echo "FSx mounts:"
df -h /fsxz/* 2>/dev/null | awk 'NR==1 || /fsxz/'
echo

echo "=== CPU info ==="
lscpu | grep -E 'Model name|Socket|Core|Thread|CPU\(s\):' | head -6
echo

echo "=== Simple parallel work (calculate pi via bash) ==="
python3 <<'PY'
import math, time
t0 = time.time()
# Leibniz series — 1천만 번 돌림
s, k = 0.0, 0
for i in range(10_000_000):
    s += (-1)**i / (2*i + 1)
pi = 4 * s
print(f"pi  ≈ {pi:.10f}")
print(f"err = {abs(pi - math.pi):.2e}")
print(f"elapsed {time.time()-t0:.2f}s")
PY

echo
echo "=== Done ==="
