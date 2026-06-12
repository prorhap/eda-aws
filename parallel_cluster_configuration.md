[English](./parallel_cluster_configuration.md) | [한국어](./parallel_cluster_configuration.ko.md)

# ParallelCluster Configuration Guide

> What `setup.sh` builds, how the cluster is configured, and how to change it.
>
> For deployment prerequisites and how to run `setup.sh`, see the
> [README](README.md).

---

## 1. What setup.sh builds

Running `setup.sh` produces the following environment end-to-end:

### 1.1 Node layout

| Node | Instance type | Count | Role |
|---|---|---|---|
| Head Node | `m7i.xlarge` | 1 (always on) | Slurm controller, job scheduler |
| Login Node | `r7i.2xlarge` | 1 (pool) | User entry point — job submission, file access |
| Compute Node | `r8i.32xlarge` | 0–2 (auto scale) | Simulation / regression workload |

- The Head Node is always running. It runs the Slurm controller (`slurmctld`) and must not be used for simulation jobs.
- The Login Node sits behind an ALB. Users SSH into it for daily work.
- Compute Nodes start automatically when jobs are submitted (`MinCount: 0`) and terminate after 15 minutes of idle time (`ScaledownIdletime: 15`).

### 1.2 Storage layout

| Mount point | Type | Use |
|---|---|---|
| `/fsxz/tools` | FSx OpenZFS | EDA tools, shared executables |
| `/fsxz/work` | FSx OpenZFS | RTL source, project files, simulation results |
| `/fsxz/scratch` | FSx OpenZFS | Per-job temporary workspace (`$USER/$SLURM_JOB_ID`) |

All three are NFS-mounted on Head, Login, and Compute nodes at boot via the
`SharedStorage` section in `pcluster-config.yaml`. Files written on any node
are immediately visible on all others.

### 1.3 Slurm configuration

| Setting | Value | Effect |
|---|---|---|
| Scheduler | Slurm | — |
| Queue name | `eda-r8i` | Used in `#SBATCH --partition=eda-r8i` |
| Capacity type | On-Demand | No spot interruption |
| Memory-based scheduling | Enabled (`CR_Core_Memory`) | `--mem` in job scripts is enforced |
| Idle scale-down | 15 min | Compute nodes terminate after 15 min idle |
| DNS | `UseEc2Hostnames: true` | Uses EC2 default hostnames; no Route 53 (VPC endpoint for Route 53 is not available) |
| Job exclusivity | `false` | Multiple jobs can share one compute node |

### 1.4 OS and storage

- **OS**: RHEL 8 (`rhel8`)
- **Head Node root volume**: 500 GiB gp3, 6000 IOPS, 250 MB/s
- **Compute Node root volume**: 200 GiB gp3, 3000 IOPS, 125 MB/s
- **FSx OpenZFS encryption**: KMS-encrypted at rest (`OpenZfsKey` in `EdaStorage` stack)

### 1.5 Monitoring

CloudWatch monitoring is enabled by default:

| Item | Setting |
|---|---|
| CloudWatch Logs | Enabled, 90-day retention |
| CloudWatch Dashboard | Enabled (cluster overview) |
| Log deletion policy | Retain (not deleted on cluster removal) |

---

## 2. pcluster-config-template.yaml structure

`setup.sh` fills the placeholder values in
`cdk/pcluster-config-template.yaml` from `cdk/outputs.json` to produce
`cdk/pcluster-config.yaml`. Understanding the template helps when you need to
make manual changes.

### 2.1 Placeholder substitution

Placeholders use stack-prefix-neutral keys. `setup.sh` reads the actual values
from `outputs.json` using the current `STACK_PREFIX`:

| Placeholder | Source in outputs.json | Value |
|---|---|---|
| `${BASE.PrimarySubnetId}` | `EdaBase.PrimarySubnetId` | Subnet for all nodes |
| `${BASE.SgClusterNodesId}` | `EdaBase.SgClusterNodesId` | Shared security group |
| `${BASE.KeyPairName}` | `EdaBase.KeyPairName` | SSH key pair name |
| `${STORAGE.VolToolsId}` | `EdaStorage.VolToolsId` | FSx volume ID for /fsxz/tools |
| `${STORAGE.VolWorkId}` | `EdaStorage.VolWorkId` | FSx volume ID for /fsxz/work |
| `${STORAGE.VolScratchId}` | `EdaStorage.VolScratchId` | FSx volume ID for /fsxz/scratch |

### 2.2 Conditional blocks

`setup.sh` activates or strips sections between marker comments based on config
flags:

| Marker | Config flag | Effect |
|---|---|---|
| `#OPENZFS_BEGIN` / `#OPENZFS_END` | `ENABLE_OPENZFS` | Include/exclude OpenZFS SharedStorage entries |
| `#ONTAP_BEGIN` / `#ONTAP_END` | `ENABLE_ONTAP` | Include/exclude ONTAP SharedStorage entries |
| `#LOGINNODES_BEGIN` / `#LOGINNODES_END` | `ENABLE_LOGIN_NODE` | Include/exclude `LoginNodes` section |
| `#SSM_BEGIN` / `#SSM_END` | `ENABLE_SSM` | Uncomment IAM policy for SSM access on Head Node |

### 2.3 Key sections at a glance

```yaml
HeadNode:
  InstanceType: m7i.xlarge          # change here to resize the head node
  LocalStorage:
    RootVolume:
      Size: 500                     # GiB — increase if /var fills up

LoginNodes:
  Pools:
    - Count: 1                      # number of concurrent login nodes
      InstanceType: r7i.2xlarge     # resize for heavier interactive work

SharedStorage:
  - MountDir: /fsxz/tools           # mount path on all nodes
    FsxOpenZfsSettings:
      VolumeId: <from outputs.json> # do not edit manually — regenerate config

Scheduling:
  SlurmSettings:
    ScaledownIdletime: 15           # minutes before idle compute terminates
    EnableMemoryBasedScheduling: true
  SlurmQueues:
    - Name: eda-r8i
      ComputeResources:
        - InstanceType: r8i.32xlarge
          MaxCount: 2               # maximum concurrent compute nodes
```

---

## 3. Changing the configuration

### 3.1 Changing cluster-level settings (instance type, node count, etc.)

Edit `cdk/pcluster-config-template.yaml`, then regenerate the config and apply:

```bash
# Regenerate pcluster-config.yaml from the updated template
SKIP_CDK=1 SKIP_CLUSTER=1 ./setup.sh

# Apply the change to the running cluster
export CLUSTER_NAME="hpc-cluster"
pcluster update-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration cdk/pcluster-config.yaml
```

Not all fields can be changed while the cluster is running. ParallelCluster
will report which changes require deleting and recreating the cluster.

### 3.2 Changing compute node instance type or count

In `pcluster-config-template.yaml`, under `SlurmQueues[0].ComputeResources`:

```yaml
ComputeResources:
  - Name: r128
    InstanceType: r8i.32xlarge   # change instance type
    MinCount: 0
    MaxCount: 2                  # change max concurrent nodes
```

Run `setup.sh` with `SKIP_CDK=1 SKIP_CLUSTER=1` and then `pcluster update-cluster`.

### 3.3 Adding a second queue

To add a queue with different instance types (e.g., a memory-optimized queue
for large regression runs):

```yaml
SlurmQueues:
  - Name: eda-r8i
    # ... existing queue ...
  - Name: eda-mem
    CapacityType: ONDEMAND
    Networking:
      SubnetIds:
        - ${BASE.PrimarySubnetId}
      AdditionalSecurityGroups:
        - ${BASE.SgClusterNodesId}
    ComputeResources:
      - Name: x2idn
        InstanceType: x2idn.32xlarge
        MinCount: 0
        MaxCount: 4
```

### 3.4 Changing FSx capacity or throughput

FSx resources are managed by CDK, not pcluster. Change the values in
`config/default.env` and redeploy the CDK storage stack:

```bash
# Edit config/default.env:
#   OPENZFS_SIZE_GIB=640
#   OPENZFS_THROUGHPUT=2560

SKIP_CLUSTER=1 ./setup.sh
```

> FSx capacity can only be increased, not decreased. Throughput can be
> changed in either direction, but only once every 6 hours.

### 3.5 Enabling SSM access after the cluster is already running

Recreate the cluster with `ENABLE_SSM=1` — SSM IAM policy cannot be patched
onto a running cluster:

```bash
pcluster delete-cluster --cluster-name $CLUSTER_NAME
# wait until deletion completes
ENABLE_SSM=1 SKIP_CDK=1 ./setup.sh
```

### 3.6 Regenerating pcluster-config.yaml without redeploying

If you only need to regenerate the config file (e.g., after editing the
template):

```bash
SKIP_CDK=1 SKIP_CLUSTER=1 ./setup.sh
```

---

## 4. Custom AMI

By default, ParallelCluster uses the official AWS-managed AMI for RHEL 8.
A custom AMI lets you bake in EDA tools, kernel parameters, and OS configuration
at image-build time, so every compute node starts clean and ready without
per-node bootstrap scripts that slow cold-start.

### 4.1 When to use a custom AMI

| Scenario | Recommended approach |
|---|---|
| EDA tools installed in a shared FSx path | Stock AMI (no custom AMI needed) |
| Kernel parameters (`vm.max_map_count`, `ulimit`) that must be set before the job | Custom AMI or `OnNodeConfigured` script |
| Large common packages (e.g., Calibre, StarRC) that are slow to install on every node | Custom AMI |
| Site-specific CA certificates, proxy config, or security agents | Custom AMI |

For this EDA environment where tools live on `/fsxz/tools`, a custom AMI is
optional but recommended if you need kernel tuning or want fully reproducible
node images.

### 4.2 Build a custom AMI with EC2 Image Builder

ParallelCluster's `pcluster build-image` command wraps EC2 Image Builder.
It starts from the official ParallelCluster base AMI and applies your
customizations, producing an AMI that is guaranteed to be compatible.

**Step 1 — Write the image configuration**

```yaml
# image-config.yaml
Build:
  InstanceType: c6i.2xlarge          # builder instance — choose one with enough RAM
  ParentImage: arn:aws:imagebuilder:ap-northeast-2:aws:image/amazon-linux-2-kernel-510-x86/x.x.x
  SubnetId: subnet-xxxxxxxxxxxxxxxxx  # same private subnet as the cluster
  SecurityGroupIds:
    - sg-xxxxxxxxxxxxxxxxx            # allow outbound HTTPS (for yum/pip); inbound not needed
  Components:
    - Type: script
      Value: |
        #!/bin/bash
        set -euo pipefail

        # ── Kernel / OS tuning ────────────────────────────────────
        cat >> /etc/sysctl.d/99-eda.conf << 'EOF'
        vm.max_map_count = 1048576
        kernel.pid_max = 4194304
        net.core.somaxconn = 65535
        EOF
        sysctl --system

        # ── ulimits for EDA tools ─────────────────────────────────
        cat >> /etc/security/limits.d/99-eda.conf << 'EOF'
        * soft nofile 1048576
        * hard nofile 1048576
        * soft nproc  unlimited
        * hard nproc  unlimited
        * soft stack  unlimited
        EOF

        # ── Site CA certificates (if any) ─────────────────────────
        # cp /path/to/site-ca.crt /etc/pki/ca-trust/source/anchors/
        # update-ca-trust

        # ── Common packages ───────────────────────────────────────
        yum install -y tcsh ksh xorg-x11-apps libXScrnSaver

DevSettings:
  Cookbook:
    ExtraChefAttributes: |
      {
        "cluster": {
          "slurm_nodename": "custom"
        }
      }
```

> Use the official ParallelCluster base AMI ARN for your region and OS.
> Find the current ARN with:
> ```bash
> pcluster list-official-images --os rhel8 --region ap-northeast-2
> ```

**Step 2 — Build the AMI**

```bash
pcluster build-image \
  --image-id eda-rhel8-$(date +%Y%m%d) \
  --image-configuration image-config.yaml \
  --region ap-northeast-2
```

The build runs as an EC2 Image Builder pipeline and takes **15–30 minutes**.
Monitor progress:

```bash
# Check build status
pcluster describe-image \
  --image-id eda-rhel8-$(date +%Y%m%d) \
  --region ap-northeast-2

# List all custom images
pcluster list-images --image-status AVAILABLE --region ap-northeast-2
```

**Step 3 — Note the AMI ID from the output**

```bash
pcluster describe-image \
  --image-id eda-rhel8-$(date +%Y%m%d) \
  --region ap-northeast-2 \
  --query 'imageConfiguration.url' --output text
# or look for ec2AmiId in the describe output
```

### 4.3 Apply the custom AMI to the cluster

In `cdk/pcluster-config-template.yaml`, replace the `Image` section:

```yaml
# Before (stock AMI)
Image:
  Os: rhel8

# After (custom AMI)
Image:
  CustomAmi: ami-0xxxxxxxxxxxxxxxxx   # AMI ID from step 4.2
```

Then regenerate the config and create (or recreate) the cluster:

```bash
# Regenerate config
SKIP_CDK=1 SKIP_CLUSTER=1 ./setup.sh

# Create a fresh cluster with the custom AMI
SKIP_CDK=1 ./setup.sh
```

> When `CustomAmi` is set, `Os` is inferred from the AMI. You can still
> specify `Os` explicitly to avoid warnings, but the custom AMI takes
> precedence.

### 4.4 Update an existing cluster to a new AMI

Changing the AMI on a running cluster requires replacing the head node, so
the cluster must be stopped first:

```bash
export CLUSTER_NAME="hpc-cluster"

# 1. Stop the compute fleet
pcluster update-compute-fleet \
  --cluster-name $CLUSTER_NAME \
  --status STOP_REQUESTED

# 2. Update the cluster configuration (triggers head node replacement)
pcluster update-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration cdk/pcluster-config.yaml

# 3. Monitor the update
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query '{Status:clusterStatus,UpdateStatus:lastUpdatedAt}'
```

> Head node replacement causes a brief Slurm controller outage (~5 min).
> Schedule this during a maintenance window.

### 4.5 AMI maintenance workflow

Custom AMIs need to be rebuilt when you update the ParallelCluster version
or apply OS patches. Recommended cadence:

1. **Monthly** — rebuild AMI with `yum update` applied
2. **On ParallelCluster minor version bump** — always rebuild; the base AMI
   changes and the old custom AMI may be incompatible
3. **On tool installation changes** — only if tools are baked into the AMI
   (tools on FSx do not require an AMI rebuild)

Keep a naming convention that encodes the build date:
`eda-rhel8-YYYYMMDD` — so you can roll back by simply pointing
`CustomAmi` to a previous AMI ID.

---

## 5. Create the cluster

### 5.1 One-shot creation via setup.sh (recommended)

`setup.sh` runs CDK deploy → config generation → SSH key download → cluster
creation in one go.

```bash
cd /path/to/eda-aws

# Default (SSH only, SSM disabled)
./setup.sh

# Enable SSM Session Manager
ENABLE_SSM=1 ./setup.sh

# Skip CDK deployment (already deployed)
SKIP_CDK=1 ./setup.sh

# Generate config file only (do not create cluster)
SKIP_CLUSTER=1 ./setup.sh
```

| Env var | Default | Description |
|---|---|---|
| `SKIP_CDK` | `0` | If `1`, skip CDK deployment |
| `SKIP_CLUSTER` | `0` | If `1`, skip cluster creation |
| `ENABLE_SSM` | `0` | If `1`, enable SSM Session Manager |

Creation takes about **10–15 minutes**, and the script waits until completion.

### 4.2 Manual creation

```bash
pcluster create-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration cdk/pcluster-config.yaml
```

### 4.3 Monitor status

```bash
# Cluster status
pcluster describe-cluster --cluster-name $CLUSTER_NAME

# Just the clusterStatus
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query 'clusterStatus'

# CloudFormation stack events
aws cloudformation describe-stack-events \
  --stack-name $CLUSTER_NAME \
  --query 'StackEvents[0:10].{Time:Timestamp,Status:ResourceStatus,Resource:LogicalResourceId}' \
  --output table
```

### 4.4 Verify creation completed

```bash
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query '{Status:clusterStatus,HeadNode:headNode.privateIpAddress,LoginAddress:loginNodes[0].address}'
```

---

## 6. Connect to the cluster

There are two methods: **SSH** and **SSM**.

| Method | Requirements | Target node | Use |
|---|---|---|---|
| SSH (via VPN) | VPN connection + SSH key pair | Head Node, Login Node | Day-to-day work, job submission |
| SSM Session Manager | IAM credentials + cluster created with `ENABLE_SSM=1` | Head Node | Admin/debug, no VPN |

### 6.1 SSH (via VPN)

SSH access requires reachability to the private IP via Site-to-Site VPN.
SSH keys are created by CDK and auto-downloaded to
`~/.ssh/eda-cluster-key.pem` when the script runs.

**Head Node access**

```bash
# Use the pcluster ssh wrapper
pcluster ssh --cluster-name $CLUSTER_NAME \
  -i ~/.ssh/eda-cluster-key.pem

# Or direct SSH
# macOS / Linux
ssh -i ~/.ssh/eda-cluster-key.pem ec2-user@<head-node-private-ip>

# Windows (PowerShell)
ssh -i $env:USERPROFILE\.ssh\eda-cluster-key.pem ec2-user@<head-node-private-ip>
```

**Login Node access (for users)**

The Login Node sits behind an NLB and inherits the HeadNode's SSH key
automatically.

```bash
# Look up the Login Node NLB address
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query 'loginNodes[0].address' --output text

# macOS / Linux
ssh -i ~/.ssh/eda-cluster-key.pem ec2-user@<login-nodes-address>

# Windows (PowerShell)
ssh -i $env:USERPROFILE\.ssh\eda-cluster-key.pem ec2-user@<login-nodes-address>
```

> In Day 1, the path is corporate network → Site-to-Site VPN → Login Node
> NLB (private IP).

**Look up Head Node private IP**

```bash
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query 'headNode.privateIpAddress' --output text
```

### 6.2 SSM Session Manager (no VPN/key needed)

SSM works with IAM credentials alone — no SSH key or VPN required. Only the
Head Node is reachable.

**Prerequisites**

1. Use the `ENABLE_SSM=1` option when creating the cluster:
   ```bash
   ENABLE_SSM=1 ./setup.sh
   ```
   This adds the `AmazonSSMManagedInstanceCore` IAM policy to the Head Node.
   The default is disabled (`ENABLE_SSM=0`).

2. Install `session-manager-plugin` locally (see README § 2.2).

**How to connect**

```bash
# Look up the Head Node instance ID
INSTANCE_ID=$(pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query 'headNode.instanceId' --output text)

# Start an SSM session
aws ssm start-session --target $INSTANCE_ID
```

> SSM connects as `ssm-user`. To switch to `ec2-user`: `sudo su - ec2-user`

---

## 7. Cluster validation tests

After cluster creation, SSH to the Login Node and run the following checks
in order.

### 7.1 Slurm status

```bash
# Partition / node status
sinfo
# Expect: eda-r8i partition in idle~ state (compute nodes not yet up)

# Detailed info
scontrol show partition
```

### 7.2 FSx mount

```bash
# Mount status
df -h | grep fsx
# Expect: /fsxz/tools, /fsxz/work, /fsxz/scratch all visible

# Mount options (nconnect, rsize/wsize)
nfsstat -m

# Read/write test
ls /fsxz/tools/
touch /fsxz/work/test_write && rm /fsxz/work/test_write
touch /fsxz/scratch/test_write && rm /fsxz/scratch/test_write
```

### 7.3 Memory-based scheduling

```bash
scontrol show config | grep -i memory
# SelectTypeParameters = CR_Core_Memory should appear
```

### 7.4 Submit a test job

Compute Nodes have `MinCount: 0`, so submitting a job auto-starts one (2–5
min).

```bash
mkdir -p /fsxz/scratch/$USER

cat > /fsxz/scratch/$USER/test_job.sh << 'EOF'
#!/bin/bash
#SBATCH --job-name=cluster-test
#SBATCH --partition=eda-r8i
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=00:10:00

echo "=== Job Info ==="
echo "Job ID: $SLURM_JOB_ID"
echo "Node: $(hostname)"
echo "CPUs: $SLURM_CPUS_PER_TASK"
echo "Memory: ${SLURM_MEM_PER_NODE:-N/A} MB"

echo ""
echo "=== Mount Points ==="
df -h | grep fsx

echo ""
echo "=== CPU Info ==="
lscpu | grep -E "^(Architecture|CPU\(s\)|Model name|Thread)"

echo ""
echo "=== Memory Info ==="
free -h
EOF

sbatch /fsxz/scratch/$USER/test_job.sh
```

### 7.5 Job status and output

```bash
# Job status (PENDING → CONFIGURING → RUNNING → COMPLETED)
squeue

# After completion
sacct --format=JobID,JobName,Partition,State,Elapsed,MaxRSS,NodeList

# Job output
cat /fsxz/scratch/$USER/slurm-*.out
```

> The Compute Node takes 2–5 min to come up while in PENDING. Watch
> transitions with `squeue`.

---

## 8. Run the first job

### 8.1 VCS simulation job example

```bash
cat > /fsxz/scratch/$USER/run_vcs.sh << 'EOF'
#!/bin/bash
#SBATCH --job-name=vcs_smoke
#SBATCH --partition=eda-r8i
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=02:00:00

WORKDIR=/fsxz/scratch/$USER/$SLURM_JOB_ID
mkdir -p $WORKDIR && cd $WORKDIR

# Tool setup
source /fsxz/tools/eda/env/vcs_setup.sh

# Copy project files
cp -r /fsxz/work/projects/chipA/rtl .
cp -r /fsxz/work/projects/chipA/tb .
cp /fsxz/work/projects/chipA/filelist/smoke.f .

# Run VCS compile + simulation
vcs -full64 -f smoke.f -o simv
./simv +ntb_random_seed=1234

# Save results
RESULT_DIR=/fsxz/work/results/chipA/$(date +%Y%m%d)/$SLURM_JOB_ID
mkdir -p $RESULT_DIR
cp -r $WORKDIR/*.log $WORKDIR/*.fsdb $RESULT_DIR/ 2>/dev/null || true

echo "Results saved to: $RESULT_DIR"
EOF

sbatch /fsxz/scratch/$USER/run_vcs.sh
```

---

## 9. Operations

### 9.1 Update the cluster

When config changes:
```bash
pcluster update-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration pcluster-config.yaml
```

### 9.2 Stop / start the cluster

To save costs while idle:
```bash
# Stop the compute fleet (head/login stay up; only compute terminates)
pcluster update-compute-fleet \
  --cluster-name $CLUSTER_NAME \
  --status STOP_REQUESTED

# Restart the compute fleet
pcluster update-compute-fleet \
  --cluster-name $CLUSTER_NAME \
  --status START_REQUESTED
```

### 9.3 Delete the cluster

```bash
pcluster delete-cluster --cluster-name $CLUSTER_NAME
# CDK resources like FSx and VPC are unaffected
```

### 9.4 Subscribe to CloudWatch alarm emails

```bash
# Look up the SNS topic ARN (from outputs.json)
ALARM_TOPIC=$(jq -r '.EdaStorage.AlarmTopicArn' cdk/outputs.json)

# Add an email subscription
aws sns subscribe \
  --topic-arn "$ALARM_TOPIC" \
  --protocol email \
  --notification-endpoint your-team@example.com
```

---

## 10. Troubleshooting

### Cluster creation fails

```bash
# Check the failure cause from CloudFormation events
pcluster describe-cluster --cluster-name $CLUSTER_NAME
# Detailed logs
pcluster get-cluster-log-events --cluster-name $CLUSTER_NAME \
  --log-stream-name cfn-init
```

### Compute Node does not start

```bash
# On the head node
sudo tail -f /var/log/parallelcluster/clustermgtd.log
sudo tail -f /var/log/parallelcluster/slurm_resume.log

# Check EC2 instance limits
aws service-quotas get-service-quota \
  --service-code ec2 \
  --quota-code L-74FC7D96
```

### FSx mount issues

```bash
# Look up FSx ID
FSX_ID=$(jq -r '.EdaStorage.FsxId' cdk/outputs.json)

# Test NFS port connectivity (from head node)
FSX_DNS=$(aws fsx describe-file-systems --file-system-ids $FSX_ID \
  --query 'FileSystems[0].DNSName' --output text)
nc -zv $FSX_DNS 2049

# Check security groups
SG_FSX=$(jq -r '.EdaBase.SgFsxId' cdk/outputs.json)
aws ec2 describe-security-groups --group-ids $SG_FSX \
  --query 'SecurityGroups[0].IpPermissions'
```

### Job stuck in PENDING

```bash
# Check the pending reason
squeue -j <job_id> -o "%R"

# Possible reasons:
# - Resources: waiting for compute node startup (2–5 min is normal)
# - Priority: another job has priority
# - ReqNodeNotAvail: node booting
```

---

## 11. VPC Flow Logs

VPC Flow Logs are auto-enabled during CDK deployment. All network traffic
(ACCEPT + REJECT) is recorded to CloudWatch Logs.

| Item | Setting |
|---|---|
| Destination | CloudWatch Logs (`/eda/vpc-flow-logs`) |
| Traffic type | ALL (ACCEPT + REJECT) |
| Retention | 90 days (default; configurable in `cdk.json`) |

Change retention:

```json
// cdk/cdk.json
"eda:vpc_flow_log_retention_days": 180
```

Supported values: `30`, `60`, `90` (default), `180`, `365`

Query with CloudWatch Logs Insights:

```
# Inspect rejected traffic
fields @timestamp, srcAddr, dstAddr, dstPort, action
| filter action = "REJECT"
| sort @timestamp desc
| limit 20
```

---

## 12. CloudTrail (audit log)

CloudTrail is auto-enabled during CDK deployment. All AWS API calls are
recorded to S3.

### 12.1 Configuration summary

| Item | Setting |
|---|---|
| Trail name | `eda-trail` |
| Log destination | `s3://eda-cloudtrail-<account-id>/` |
| Encryption | KMS (`eda/cloudtrail`) |
| Target events | Management events (read + write) |
| S3 lifecycle | 90 days → Glacier, 365 days → Delete |
| Cost | Management events for 1 trail are free; S3 storage is minimal |

### 12.2 Inspect logs

```bash
# Recent events (CLI instead of the CloudTrail console)
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=ec2.amazonaws.com \
  --max-results 10 \
  --query 'Events[].{Time:EventTime,Name:EventName,User:Username}'
```

### 12.3 Query with Athena

In the CloudTrail console → Event history → Create Athena table to
auto-create the table, then query with SQL in Athena.

```sql
-- Who started EC2 instances
SELECT eventtime, useridentity.arn, requestparameters
FROM cloudtrail_logs
WHERE eventsource = 'ec2.amazonaws.com'
  AND eventname = 'RunInstances'
ORDER BY eventtime DESC
LIMIT 10;

-- Security group change history
SELECT eventtime, useridentity.arn, eventname, requestparameters
FROM cloudtrail_logs
WHERE eventsource = 'ec2.amazonaws.com'
  AND eventname LIKE '%SecurityGroup%'
ORDER BY eventtime DESC;

-- Recent activity for a specific user
SELECT eventtime, eventsource, eventname
FROM cloudtrail_logs
WHERE useridentity.arn LIKE '%username%'
ORDER BY eventtime DESC
LIMIT 20;
```

---

## 13. DCV remote desktop

Per-user DCV instances for EDA GUI work (Verdi, DVE, etc.) are managed by a
separate CDK project (`cdk-dcv/`). It is common to keep the Login Node
(SSH job submission) and the DCV instances (GUI work) separate.

### 13.1 Structure

```
cdk/       → VPC, FSx, SG → SSM Parameter Store (/eda/network/*, /eda/storage/*)
cdk-dcv/   → Reads from SSM and creates DCV EC2 instances (per-user independent stacks)
```

### 13.2 Deploy a DCV instance

```bash
cd cdk-dcv/

# Per-user DCV instance creation
./deploy-dcv.sh alice
./deploy-dcv.sh bob
./deploy-dcv.sh alice r7i.4xlarge   # specify instance type (default: r7i.2xlarge)
```

Deployment automatically:
- References the main CDK's VPC/Subnet/SG via SSM
- NFS-mounts FSx volumes (`/fsxz/tools`, `/fsxz/work`, `/fsxz/scratch`)
- Installs the DCV server + sets up the GUI desktop
- Records the instance ID into SSM Parameter (`/eda/dcv/<username>/InstanceId`)
- Sets the EC2 tag `User: <username>`

### 13.3 Connect to DCV

Connect via VPN in your browser:

```
https://<private-ip>:8443
```

DCV login info:
- Username: the user name specified at deployment
- Password: `changeme123` (initial; change after first login)

SSH:
```bash
ssh -i ~/.ssh/eda-cluster-key.pem ec2-user@<private-ip>
```

### 13.4 Manage DCV instances

```bash
# Look up instance ID
INSTANCE_ID=$(aws ssm get-parameter --name /eda/dcv/alice/InstanceId \
  --query 'Parameter.Value' --output text)

# Stop (cost saving)
aws ec2 stop-instances --instance-ids $INSTANCE_ID

# Start
aws ec2 start-instances --instance-ids $INSTANCE_ID

# List all DCV instances
aws ec2 describe-instances \
  --filters "Name=tag:User,Values=*" "Name=tag:Name,Values=dcv-*" \
  --query 'Reservations[].Instances[].{User:Tags[?Key==`User`]|[0].Value,Id:InstanceId,State:State.Name,Ip:PrivateIpAddress}' \
  --output table
```

### 13.5 Delete a DCV instance

```bash
./destroy-dcv.sh alice
```

---

## 14. CDK settings (cdk.json)

Project settings configurable in `cdk/cdk.json`:

| Key | Default | Description |
|---|---|---|
| `eda:stack_prefix` | `Eda` | Stack prefix — `{prefix}Base`, `{prefix}Storage`, `{prefix}LicenseServer` |
| `eda:base_stack_name` | `{prefix}Base` | Override Base stack name individually |
| `eda:storage_stack_name` | `{prefix}Storage` | Override Storage stack name individually |
| `eda:license_stack_name` | `{prefix}LicenseServer` | Override License stack name individually |
| `eda:vpc_flow_log_retention_days` | `90` | VPC Flow Log retention (days) |

> Changing the stack name creates a new stack. The existing stack must be
> deleted before redeploying.

---

## 15. SSM Parameter Store layout

SSM parameters published by the main CDK (referenced by external projects
like DCV):

| Parameter | Source | Use |
|---|---|---|
| `/eda/network/VpcId` | {prefix}Base | VPC ID |
| `/eda/network/PrimarySubnetId` | {prefix}Base | Private subnet ID |
| `/eda/network/PrimaryAz` | {prefix}Base | Subnet AZ |
| `/eda/network/SgClusterNodesId` | {prefix}Base | Cluster node SG |
| `/eda/network/KeyPairName` | {prefix}Base | SSH KeyPair name |
| `/eda/storage/FsxDns` | EdaStorage | FSx DNS endpoint |
| `/eda/storage/VolToolsId` | EdaStorage | /fsxz/tools volume ID |
| `/eda/storage/VolWorkId` | EdaStorage | /fsxz/work volume ID |
| `/eda/storage/VolScratchId` | EdaStorage | /fsxz/scratch volume ID |
| `/eda/dcv/<user>/InstanceId` | cdk-dcv | DCV instance ID (per user) |

```bash
# List all parameters
aws ssm get-parameters-by-path --path /eda/ --recursive \
  --query 'Parameters[].{Name:Name,Value:Value}' --output table
```

---

## 16. Clean reinstall

Procedure to remove the entire environment and reinstall from scratch:

```bash
# 1. Delete DCV instances (if any)
cd cdk-dcv/
./destroy-dcv.sh alice
./destroy-dcv.sh bob

# 2. Delete the ParallelCluster (~10 min)
pcluster delete-cluster --cluster-name $CLUSTER_NAME
# Wait until "does not exist"
watch -n 30 'pcluster describe-cluster --cluster-name $CLUSTER_NAME 2>&1 | head -3'

# 3. Delete CDK stacks (FSx deletion takes 20–30 min)
cd cdk/
cdk destroy --all

# 4. Redeploy
cd ..
./setup.sh
```

> FSx and KMS keys are set to `RemovalPolicy.RETAIN` and are not removed by
> `cdk destroy`. Delete them manually via the AWS console or CLI to fully
> remove them.

---

## 17. Cost management tips

1. **Stop the compute fleet when not in use** — keeping only head/login
   greatly reduces cost
2. **Stop DCV instances** — save cost when idle with `aws ec2 stop-instances`
3. **ScaledownIdletime=15** — idle compute nodes auto-terminate after 15 min
4. **Monitor FSx throughput** — if CloudWatch
   `NetworkThroughputUtilization` stays above 50%, consider 512 → 1024 MBps
5. **No nighttime / weekend jobs** — automate compute fleet STOP via
   EventBridge + Lambda

---

## References

- [AWS ParallelCluster User Guide](https://docs.aws.amazon.com/parallelcluster/latest/ug/)
- [pcluster CLI Reference](https://docs.aws.amazon.com/parallelcluster/latest/ug/pcluster-v3.html)
- [FSx for OpenZFS Performance](https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/performance.html)
- [Amazon DCV Admin Guide](https://docs.aws.amazon.com/dcv/latest/adminguide/)
- This project's `architecture_guide.md` v1.2
