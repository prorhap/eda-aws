[English](./parallelcluster_guide.md) | [한국어](./parallelcluster_guide.ko.md)

# ParallelCluster Creation Guide

> A practical guide for creating a ParallelCluster on top of infrastructure
> (VPC, FSx, Security Groups) deployed by CDK.
>
> This document is based on the Day 1 setup of `architecture_guide.md` v1.2.

---

## 1. Prerequisites

### 1.1 Install required tools

**AWS CLI v2**

```bash
# macOS
brew install awscli

# Linux (x86_64)
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip && sudo ./aws/install

# Windows
# Download and install https://awscli.amazonaws.com/AWSCLIV2.msi

aws --version
```

**SSM Session Manager Plugin** (required for SSM-based access)

```bash
# macOS
brew install --cask session-manager-plugin

# Linux — Debian/Ubuntu (x86_64)
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
  -o session-manager-plugin.deb
sudo dpkg -i session-manager-plugin.deb

# Linux — RHEL/Amazon Linux (x86_64)
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/64bit/session-manager-plugin.rpm" \
  -o session-manager-plugin.rpm
sudo yum install -y session-manager-plugin.rpm

# Windows
# https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe

session-manager-plugin --version
```

**ParallelCluster CLI**

```bash
pip install "aws-parallelcluster"
pcluster version
```

**Other required tools**

```bash
# jq — JSON processing
# macOS
brew install jq
# Linux — Debian/Ubuntu
sudo apt-get install -y jq
# Linux — RHEL/Amazon Linux
sudo yum install -y jq

# Node.js (required for the CDK CLI) — install LTS from https://nodejs.org/
node --version
npm --version
```

### 1.2 AWS credentials

```bash
# Verify current account/region
aws sts get-caller-identity
aws configure get region   # must be ap-northeast-2

# If not, set it (then --region can be omitted in subsequent commands)
aws configure set region ap-northeast-2
```

> All commands in this document assume the default region is set to
> `ap-northeast-2`. If not, add `--region ap-northeast-2` to each command, or
> set the default region via the command above.

```bash
# Set the cluster name (referenced as $CLUSTER_NAME below)
export CLUSTER_NAME="hpc-cluster"
```

> All CLI commands in this document use the `$CLUSTER_NAME` environment
> variable. Run the command above first, then copy-paste the rest.

### 1.3 Check CDK deployment status

```bash
# CDK deployment must already be complete
cd cdk/
cat outputs.json
```

The following values must exist in outputs.json (with `STACK_PREFIX=Eda`):

| Key | Use |
|---|---|
| `EdaBase.PrimarySubnetId` | Subnet for Head/Login/Compute |
| `EdaBase.SgClusterNodesId` | Cluster node security group |
| `EdaBase.KeyPairName` | SSH KeyPair name |
| `EdaBase.KeyPairId` | SSH KeyPair ID (used to fetch private key from SSM) |
| `EdaStorage.VolToolsId` | /fsx/tools volume ID |
| `EdaStorage.VolWorkId` | /fsx/work volume ID |
| `EdaStorage.VolScratchId` | /fsx/scratch volume ID |

---

## 2. Create the ParallelCluster config file

### 2.1 Generate the config file from the template

Replace the `${...}` placeholders in `pcluster-config-template.yaml` with
real values.

```bash
cd cdk/

# Auto-substitute values from outputs.json.
# Template placeholders are stack-prefix-neutral keys (${BASE.*}, ${STORAGE.*}),
# and the actual values come from the stack names in outputs.json (default
# EdaBase / EdaStorage).
SUBNET_ID=$(jq -r '.EdaBase.PrimarySubnetId' outputs.json)
SG_CLUSTER=$(jq -r '.EdaBase.SgClusterNodesId' outputs.json)
KEY_PAIR_NAME=$(jq -r '.EdaBase.KeyPairName' outputs.json)
VOL_TOOLS=$(jq -r '.EdaStorage.VolToolsId' outputs.json)
VOL_WORK=$(jq -r '.EdaStorage.VolWorkId' outputs.json)
VOL_SCRATCH=$(jq -r '.EdaStorage.VolScratchId' outputs.json)

sed \
  -e "s|\${BASE.PrimarySubnetId}|${SUBNET_ID}|g" \
  -e "s|\${BASE.SgClusterNodesId}|${SG_CLUSTER}|g" \
  -e "s|\${BASE.KeyPairName}|${KEY_PAIR_NAME}|g" \
  -e "s|\${STORAGE.VolToolsId}|${VOL_TOOLS}|g" \
  -e "s|\${STORAGE.VolWorkId}|${VOL_WORK}|g" \
  -e "s|\${STORAGE.VolScratchId}|${VOL_SCRATCH}|g" \
  pcluster-config-template.yaml > pcluster-config.yaml

echo "Generated: pcluster-config.yaml"
```

### 2.2 Validate the generated config file

```bash
# Verify no placeholders remain
grep '${' pcluster-config.yaml && echo "ERROR: unsubstituted values found" || echo "OK: all values substituted"

# Validate the ParallelCluster config
pcluster configure validate --config pcluster-config.yaml
```

### 2.3 Inspect current values

```bash
# View all values from outputs.json
jq '.' outputs.json
```

> You don't need to create the config file manually — `setup.sh` automates
> this step.

---

## 3. Create the cluster

### 3.1 One-shot creation via setup.sh (recommended)

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

### 3.2 Manual creation

```bash
pcluster create-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration cdk/pcluster-config.yaml
```

### 3.3 Monitor status

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

### 3.4 Verify creation completed

```bash
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query '{Status:clusterStatus,HeadNode:headNode.privateIpAddress,LoginAddress:loginNodes[0].address}'
```

---

## 4. Connect to the cluster

There are two methods: **SSH** and **SSM**.

| Method | Requirements | Target node | Use |
|---|---|---|---|
| SSH (via VPN) | VPN connection + SSH key pair | Head Node, Login Node | Day-to-day work, job submission |
| SSM Session Manager | IAM credentials + cluster created with `ENABLE_SSM=1` | Head Node | Admin/debug, no VPN |

### 4.1 SSH (via VPN)

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

### 4.2 SSM Session Manager (no VPN/key needed)

SSM works with IAM credentials alone — no SSH key or VPN required. Only the
Head Node is reachable.

**Prerequisites**

1. Use the `ENABLE_SSM=1` option when creating the cluster:
   ```bash
   ENABLE_SSM=1 ./setup.sh
   ```
   This adds the `AmazonSSMManagedInstanceCore` IAM policy to the Head Node.
   The default is disabled (`ENABLE_SSM=0`).

2. Install `session-manager-plugin` locally:
   ```bash
   # macOS
   brew install --cask session-manager-plugin

   # Linux — Debian/Ubuntu
   curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
     -o session-manager-plugin.deb
   sudo dpkg -i session-manager-plugin.deb

   # Linux — RHEL/Amazon Linux
   curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/64bit/session-manager-plugin.rpm" \
     -o session-manager-plugin.rpm
   sudo yum install -y session-manager-plugin.rpm
   ```

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

## 5. Cluster validation tests

After cluster creation, SSH to the Login Node and run the following checks
in order.

### 5.1 Slurm status

```bash
# Partition / node status
sinfo
# Expect: eda-r8i partition in idle~ state (compute nodes not yet up)

# Detailed info
scontrol show partition
```

### 5.2 FSx mount

```bash
# Mount status
df -h | grep fsx
# Expect: /fsx/tools, /fsx/work, /fsx/scratch all visible

# Mount options (nconnect, rsize/wsize)
nfsstat -m

# Read/write test
ls /fsx/tools/
touch /fsx/work/test_write && rm /fsx/work/test_write
touch /fsx/scratch/test_write && rm /fsx/scratch/test_write
```

### 5.3 Memory-based scheduling

```bash
scontrol show config | grep -i memory
# SelectTypeParameters = CR_Core_Memory should appear
```

### 5.4 Submit a test job

Compute Nodes have `MinCount: 0`, so submitting a job auto-starts one (2–5
min).

```bash
mkdir -p /fsx/scratch/$USER

cat > /fsx/scratch/$USER/test_job.sh << 'EOF'
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

sbatch /fsx/scratch/$USER/test_job.sh
```

### 5.5 Job status and output

```bash
# Job status (PENDING → CONFIGURING → RUNNING → COMPLETED)
squeue

# After completion
sacct --format=JobID,JobName,Partition,State,Elapsed,MaxRSS,NodeList

# Job output
cat /fsx/scratch/$USER/slurm-*.out
```

> The Compute Node takes 2–5 min to come up while in PENDING. Watch
> transitions with `squeue`.

---

## 6. Run the first job

### 6.3 VCS simulation job example

```bash
cat > /fsx/scratch/$USER/run_vcs.sh << 'EOF'
#!/bin/bash
#SBATCH --job-name=vcs_smoke
#SBATCH --partition=eda-r8i
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=02:00:00

WORKDIR=/fsx/scratch/$USER/$SLURM_JOB_ID
mkdir -p $WORKDIR && cd $WORKDIR

# Tool setup
source /fsx/tools/eda/env/vcs_setup.sh

# Copy project files
cp -r /fsx/work/projects/chipA/rtl .
cp -r /fsx/work/projects/chipA/tb .
cp /fsx/work/projects/chipA/filelist/smoke.f .

# Run VCS compile + simulation
vcs -full64 -f smoke.f -o simv
./simv +ntb_random_seed=1234

# Save results
RESULT_DIR=/fsx/work/results/chipA/$(date +%Y%m%d)/$SLURM_JOB_ID
mkdir -p $RESULT_DIR
cp -r $WORKDIR/*.log $WORKDIR/*.fsdb $RESULT_DIR/ 2>/dev/null || true

echo "Results saved to: $RESULT_DIR"
EOF

sbatch /fsx/scratch/$USER/run_vcs.sh
```

---

## 7. Operations

### 7.1 Update the cluster

When config changes:
```bash
pcluster update-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration pcluster-config.yaml
```

### 7.2 Stop / start the cluster

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

### 7.3 Delete the cluster

```bash
pcluster delete-cluster --cluster-name $CLUSTER_NAME
# CDK resources like FSx and VPC are unaffected
```

### 7.4 Subscribe to CloudWatch alarm emails

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

## 8. Day 1 checklist

Items to verify within the first 2–3 days after cluster creation:

- [ ] Run the validation tests from section 5 (Slurm, FSx, test job)
- [ ] Verify compute node auto start/stop (ScaledownIdletime=15 min)
- [ ] Verify Login Node SSH access
- [ ] Verify VCS compile + simulation works
- [ ] Verify the `/fsx/scratch/$USER/$SLURM_JOB_ID` workdir pattern
- [ ] Check FSx CloudWatch metrics (AWS/FSx namespace in the console)
- [ ] Configure alarm email subscriptions
- [ ] Deploy and connect to a DCV instance (when GUI work is required)
- [ ] Verify CloudTrail event recording
- [ ] Verify VPC Flow Logs recording

---

## 9. Troubleshooting

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

## 10. VPC Flow Logs

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

## 11. CloudTrail (audit log)

CloudTrail is auto-enabled during CDK deployment. All AWS API calls are
recorded to S3.

### 11.1 Configuration summary

| Item | Setting |
|---|---|
| Trail name | `eda-trail` |
| Log destination | `s3://eda-cloudtrail-<account-id>/` |
| Encryption | KMS (`eda/cloudtrail`) |
| Target events | Management events (read + write) |
| S3 lifecycle | 90 days → Glacier, 365 days → Delete |
| Cost | Management events for 1 trail are free; S3 storage is minimal |

### 11.2 Inspect logs

```bash
# Recent events (CLI instead of the CloudTrail console)
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=ec2.amazonaws.com \
  --max-results 10 \
  --query 'Events[].{Time:EventTime,Name:EventName,User:Username}'
```

### 11.3 Query with Athena

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

## 12. DCV remote desktop

Per-user DCV instances for EDA GUI work (Verdi, DVE, etc.) are managed by a
separate CDK project (`cdk-dcv/`). It is common to keep the Login Node
(SSH job submission) and the DCV instances (GUI work) separate.

### 12.1 Structure

```
cdk/       → VPC, FSx, SG → SSM Parameter Store (/eda/network/*, /eda/storage/*)
cdk-dcv/   → Reads from SSM and creates DCV EC2 instances (per-user independent stacks)
```

### 12.2 Deploy a DCV instance

```bash
cd cdk-dcv/

# Per-user DCV instance creation
./deploy-dcv.sh alice
./deploy-dcv.sh bob
./deploy-dcv.sh alice r7i.4xlarge   # specify instance type (default: r7i.2xlarge)
```

Deployment automatically:
- References the main CDK's VPC/Subnet/SG via SSM
- NFS-mounts FSx volumes (`/fsx/tools`, `/fsx/work`, `/fsx/scratch`)
- Installs the DCV server + sets up the GUI desktop
- Records the instance ID into SSM Parameter (`/eda/dcv/<username>/InstanceId`)
- Sets the EC2 tag `User: <username>`

### 12.3 Connect to DCV

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

### 12.4 Manage DCV instances

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

### 12.5 Delete a DCV instance

```bash
./destroy-dcv.sh alice
```

---

## 13. CDK settings (cdk.json)

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

## 14. SSM Parameter Store layout

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
| `/eda/storage/VolToolsId` | EdaStorage | /fsx/tools volume ID |
| `/eda/storage/VolWorkId` | EdaStorage | /fsx/work volume ID |
| `/eda/storage/VolScratchId` | EdaStorage | /fsx/scratch volume ID |
| `/eda/dcv/<user>/InstanceId` | cdk-dcv | DCV instance ID (per user) |

```bash
# List all parameters
aws ssm get-parameters-by-path --path /eda/ --recursive \
  --query 'Parameters[].{Name:Name,Value:Value}' --output table
```

---

## 15. Clean reinstall

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

## 16. Cost management tips

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
